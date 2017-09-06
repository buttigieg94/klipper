#!/usr/bin/env python2
# Main code for host side printer firmware
#
# Copyright (C) 2016,2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys, optparse, ConfigParser, logging, time, threading
import util, reactor, queuelogger, msgproto, gcode
import pins, mcu, chipmisc, toolhead, extruder, fan, heater, servo

message_ready = "Printer is ready"

message_startup = """
The klippy host software is attempting to connect.  Please
retry in a few moments.
Printer is not ready
"""

message_restart = """
Once the underlying issue is corrected, use the "RESTART"
command to reload the config and restart the host software.
Printer is halted
"""

message_protocol_error = """
This type of error is frequently caused by running an older
version of the firmware on the micro-controller (fix by
recompiling and flashing the firmware).
Once the underlying issue is corrected, use the "RESTART"
command to reload the config and restart the host software.
Protocol error connecting to printer
"""

message_mcu_connect_error = """
Once the underlying issue is corrected, use the
"FIRMWARE_RESTART" command to reset the firmware, reload the
config, and restart the host software.
Error configuring printer
"""

message_shutdown = """
Once the underlying issue is corrected, use the
"FIRMWARE_RESTART" command to reset the firmware, reload the
config, and restart the host software.
Printer is shutdown
"""

class ConfigWrapper:
    error = ConfigParser.Error
    class sentinel:
        pass
    def __init__(self, printer, section):
        self.printer = printer
        self.section = section
    def get_wrapper(self, parser, option, default
                    , minval=None, maxval=None, above=None, below=None):
        if (default is not self.sentinel
            and not self.printer.fileconfig.has_option(self.section, option)):
            return default
        self.printer.all_config_options[
            (self.section.lower(), option.lower())] = 1
        try:
            v = parser(self.section, option)
        except self.error as e:
            raise
        except:
            raise self.error("Unable to parse option '%s' in section '%s'" % (
                option, self.section))
        if minval is not None and v < minval:
            raise self.error(
                "Option '%s' in section '%s' must have minimum of %s" % (
                    option, self.section, minval))
        if maxval is not None and v > maxval:
            raise self.error(
                "Option '%s' in section '%s' must have maximum of %s" % (
                    option, self.section, maxval))
        if above is not None and v <= above:
            raise self.error(
                "Option '%s' in section '%s' must be above %s" % (
                    option, self.section, above))
        if below is not None and v >= below:
            raise self.error(
                "Option '%s' in section '%s' must be below %s" % (
                    option, self.section, below))
        return v
    def get(self, option, default=sentinel):
        return self.get_wrapper(self.printer.fileconfig.get, option, default)
    def getint(self, option, default=sentinel, minval=None, maxval=None):
        return self.get_wrapper(
            self.printer.fileconfig.getint, option, default, minval, maxval)
    def getfloat(self, option, default=sentinel
                 , minval=None, maxval=None, above=None, below=None):
        return self.get_wrapper(
            self.printer.fileconfig.getfloat, option, default
            , minval, maxval, above, below)
    def getboolean(self, option, default=sentinel):
        return self.get_wrapper(
            self.printer.fileconfig.getboolean, option, default)
    def getchoice(self, option, choices, default=sentinel):
        c = self.get(option, default)
        if c not in choices:
            raise self.error(
                "Option '%s' in section '%s' is not a valid choice" % (
                    option, self.section))
        return choices[c]
    def getsection(self, section):
        return ConfigWrapper(self.printer, section)
    def has_section(self, section):
        return self.printer.fileconfig.has_section(section)
    def get_prefix_sections(self, prefix):
        return [self.getsection(s) for s in self.printer.fileconfig.sections()
                if s.startswith(prefix)]

class ConfigLogger():
    def __init__(self, cfg, bglogger):
        self.lines = ["===== Config file ====="]
        cfg.write(self)
        self.lines.append("=======================")
        data = "\n".join(self.lines)
        logging.info(data)
        bglogger.set_rollover_info("config", data)
    def write(self, data):
        self.lines.append(data.strip())

class Printer:
    def __init__(self, input_fd, bglogger, start_args):
        self.bglogger = bglogger
        self.start_args = start_args
        if bglogger is not None:
            bglogger.set_rollover_info("config", None)
        self.reactor = reactor.Reactor()
        self.objects = {}
        self.gcode = gcode.GCodeParser(self, input_fd)
        self.stats_timer = self.reactor.register_timer(self._stats)
        self.connect_timer = self.reactor.register_timer(
            self._connect, self.reactor.NOW)
        self.all_config_options = {}
        self.need_dump_debug = False
        self.state_message = message_startup
        self.run_result = None
        self.fileconfig = None
        self.mcu = None
    def get_start_args(self):
        return self.start_args
    def _stats(self, eventtime, force_output=False):
        if self.need_dump_debug:
            # Call dump_debug here so it is executed in the main thread
            self.gcode.dump_debug()
            self.need_dump_debug = False
        toolhead = self.objects.get('toolhead')
        if toolhead is None or self.mcu is None:
            return eventtime + 1.
        is_active, thstats = toolhead.stats(eventtime)
        if not is_active and not force_output:
            return eventtime + 1.
        out = []
        out.append(self.gcode.stats(eventtime))
        out.append(thstats)
        out.append(self.mcu.stats(eventtime))
        logging.info("Stats %.1f: %s" % (eventtime, ' '.join(out)))
        return eventtime + 1.
    def add_object(self, name, obj):
        self.objects[name] = obj
    def _load_config(self):
        self.fileconfig = ConfigParser.RawConfigParser()
        config_file = self.start_args['config_file']
        res = self.fileconfig.read(config_file)
        if not res:
            raise ConfigParser.Error("Unable to open config file %s" % (
                config_file,))
        if self.bglogger is not None:
            ConfigLogger(self.fileconfig, self.bglogger)
        # Create printer components
        config = ConfigWrapper(self, 'printer')
        for m in [pins, mcu, chipmisc, toolhead, extruder, fan, heater, servo]:
            m.add_printer_objects(self, config)
        self.mcu = self.objects['mcu']
        # Validate that there are no undefined parameters in the config file
        valid_sections = { s: 1 for s, o in self.all_config_options }
        for section in self.fileconfig.sections():
            section = section.lower()
            if section not in valid_sections:
                raise ConfigParser.Error("Unknown config file section '%s'" % (
                    section,))
            for option in self.fileconfig.options(section):
                option = option.lower()
                if (section, option) not in self.all_config_options:
                    raise ConfigParser.Error(
                        "Unknown option '%s' in section '%s'" % (
                            option, section))
    def _connect(self, eventtime):
        try:
            self._load_config()
            if self.start_args.get('debugoutput') is None:
                self.reactor.update_timer(self.stats_timer, self.reactor.NOW)
            self.mcu.connect()
            self.gcode.set_printer_ready(True)
            self.state_message = message_ready
        except (ConfigParser.Error, pins.error) as e:
            logging.exception("Config error")
            self.state_message = "%s%s" % (str(e), message_restart)
            self.reactor.update_timer(self.stats_timer, self.reactor.NEVER)
        except msgproto.error as e:
            logging.exception("Protocol error")
            self.state_message = "%s%s" % (str(e), message_protocol_error)
            self.reactor.update_timer(self.stats_timer, self.reactor.NEVER)
        except mcu.error as e:
            logging.exception("MCU error during connect")
            self.state_message = "%s%s" % (str(e), message_mcu_connect_error)
            self.reactor.update_timer(self.stats_timer, self.reactor.NEVER)
        except:
            logging.exception("Unhandled exception during connect")
            self.state_message = "Internal error during connect.%s" % (
                message_restart,)
            self.reactor.update_timer(self.stats_timer, self.reactor.NEVER)
        self.reactor.unregister_timer(self.connect_timer)
        return self.reactor.NEVER
    def run(self):
        systime = time.time()
        monotime = self.reactor.monotonic()
        logging.info("Start printer at %s (%.1f %.1f)" % (
            time.asctime(time.localtime(systime)), systime, monotime))
        # Enter main reactor loop
        try:
            self.reactor.run()
        except:
            logging.exception("Unhandled exception during run")
            return "exit"
        # Check restart flags
        run_result = self.run_result
        try:
            self._stats(self.reactor.monotonic(), force_output=True)
            if self.mcu is not None:
                if run_result == 'firmware_restart':
                    self.mcu.microcontroller_restart()
                self.mcu.disconnect()
        except:
            logging.exception("Unhandled exception during post run")
        return run_result
    def get_state_message(self):
        return self.state_message
    def note_shutdown(self, msg):
        if self.state_message == message_ready:
            self.need_dump_debug = True
        self.state_message = "%s%s" % (msg, message_shutdown)
        self.gcode.set_printer_ready(False)
    def note_mcu_error(self, msg):
        self.state_message = "%s%s" % (msg, message_restart)
        self.gcode.set_printer_ready(False)
        self.gcode.motor_heater_off()
    def request_exit(self, result="exit"):
        self.run_result = result
        self.reactor.end()


######################################################################
# Startup
######################################################################

def main():
    usage = "%prog [options] <config file>"
    opts = optparse.OptionParser(usage)
    opts.add_option("-i", "--debuginput", dest="debuginput",
                    help="read commands from file instead of from tty port")
    opts.add_option("-I", "--input-tty", dest="inputtty", default='/tmp/printer',
                    help="input tty name (default is /tmp/printer)")
    opts.add_option("-l", "--logfile", dest="logfile",
                    help="write log to file instead of stderr")
    opts.add_option("-v", action="store_true", dest="verbose",
                    help="enable debug messages")
    opts.add_option("-o", "--debugoutput", dest="debugoutput",
                    help="write output to file instead of to serial port")
    opts.add_option("-d", "--dictionary", dest="dictionary",
                    help="file to read for mcu protocol dictionary")
    options, args = opts.parse_args()
    if len(args) != 1:
        opts.error("Incorrect number of arguments")
    start_args = {'config_file': args[0], 'start_reason': 'startup'}

    input_fd = bglogger = None

    debuglevel = logging.INFO
    if options.verbose:
        debuglevel = logging.DEBUG
    if options.debuginput:
        start_args['debuginput'] = options.debuginput
        debuginput = open(options.debuginput, 'rb')
        input_fd = debuginput.fileno()
    else:
        input_fd = util.create_pty(options.inputtty)
    if options.debugoutput:
        start_args['debugoutput'] = options.debugoutput
        start_args['dictionary'] = options.dictionary
    if options.logfile:
        bglogger = queuelogger.setup_bg_logging(options.logfile, debuglevel)
    else:
        logging.basicConfig(level=debuglevel)
    logging.info("Starting Klippy...")
    start_args['software_version'] = util.get_git_version()
    if bglogger is not None:
        lines = ["Args: %s" % (sys.argv,),
                 "Git version: %s" % (repr(start_args['software_version']),),
                 "CPU: %s" % (util.get_cpu_info(),),
                 "Python: %s" % (repr(sys.version),)]
        lines = "\n".join(lines)
        logging.info(lines)
        bglogger.set_rollover_info('versions', lines)

    # Start Printer() class
    while 1:
        printer = Printer(input_fd, bglogger, start_args)
        res = printer.run()
        if res == 'exit':
            break
        time.sleep(1.)
        logging.info("Restarting printer")
        start_args['start_reason'] = res

    if bglogger is not None:
        bglogger.stop()

if __name__ == '__main__':
    main()
