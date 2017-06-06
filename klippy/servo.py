# Printer servo support
#
# Copyright (C) 2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import pins, extruder

SERVO_MIN_TIME = 0.100
SERVO_SIGNAL_PERIOD = 0.020

class PrinterServo:
    def __init__(self, printer, config):
        self.mcu_servo = pins.setup_pin(printer, 'pwm', config.get('pin'))
        self.mcu_servo.setup_max_duration(0.)
        self.mcu_servo.setup_cycle_time(SERVO_SIGNAL_PERIOD)
        self.min_width = config.getfloat(
            'minimum_pulse_width', .001, above=0., below=SERVO_SIGNAL_PERIOD)
        self.max_width = config.getfloat(
            'maximum_pulse_width', .002
            , above=self.min_width, below=SERVO_SIGNAL_PERIOD)
        self.max_angle = config.getfloat('maximum_servo_angle', 180.)
        self.angle_to_width = (self.max_width - self.min_width) / self.max_angle
        self.width_to_value = 1. / SERVO_SIGNAL_PERIOD
        self.last_value = self.last_value_time = 0.
    def set_pwm(self, print_time, value):
        if value == self.last_value:
            return
        mcu_time = self.mcu_servo.get_mcu().print_to_mcu_time(print_time)
        mcu_time = max(self.last_value_time + SERVO_MIN_TIME, mcu_time)
        self.mcu_servo.set_pwm(mcu_time, value)
        self.last_value = value
        self.last_value_time = mcu_time
    # External commands
    def set_angle(self, print_time, angle):
        angle = max(0., min(self.max_angle, angle))
        width = self.min_width + angle * self.angle_to_width
        self.set_pwm(print_time, width * self.width_to_value)
    def set_pulse_width(self, print_time, width):
        width = max(self.min_width, min(self.max_width, width))
        self.set_pwm(print_time, width * self.width_to_value)

def add_printer_objects(printer, config):
    for s in config.get_prefix_sections('servo '):
        printer.add_object(s.section, PrinterServo(printer, s))

def get_printer_servo(printer, name):
    return printer.objects.get('servo ' + name)
