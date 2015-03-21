# (C) 2015  Kyoto University Mechatronics Laboratory
# Released under the GNU General Public License, version 3
"""
Manage motor speeds for the robot.

The client obtains motor speeds from the base station, and relays them to the
motors. If the received speed for a flipper is zero, it attempts to hold the
position based on encoder data received from the mbed.

"""
import logging
import pickle
import socket

from common.exceptions import NoDriversError, MotorCountError, NoSerialsError
from common.networking import get_ip_address


class Client(object):
    """
    A client to communicate with the base station and control the robot.

    Parameters
    ----------
    server_address : 2-tuple of (str, int)
        The address at which the server is listening. The elements are the
        server address and the port number respectively.

    Attributes
    ----------
    request : socket
        Handles communication with the server.
    server_address : 2-tuple of (str, int)
        The address at which the server is listening. The elements are the
        server address and the port number respectively.
    motors : dict
        Contains all registered motors.

        **Dictionary format :** {name (str): motor (Motor)}
    serials : dict
        Contains all registered serial connections.

        **Dictionary format :** {name (str): connection (Serial)}

    Examples
    --------
    >>> client = TCPClientBase(("192.168.11.1", 22))
    >>> client.run()

    """
    def __init__(self, server_address):
        # Get local IP address.
        try:
            ip_address = get_ip_address("eth0")
        except OSError:
            ip_address = get_ip_address("enp2s0")

        self.logger = logging.getLogger("{}_client".format(ip_address))
        self.logger.debug("Creating client")
        self.request = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.request.connect(server_address)
        self.logger.info("Connected to {}:{}".format(server_address[0],
                                                     server_address[1]))
        self.request.settimeout(0.5)  # seconds
        self.server_address = server_address
        self._sensors_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.motors = {}
        self.serials = {}

    def run(self):
        """
        Send and handle requests until a ``KeyboardInterrupt`` is received.

        This method connects to the server, and loops forever. It takes the
        speed data from the base station, and manages motor outputs. It
        attempts to hold the flipper position if there is no input.

        If the connection is lost, it shuts down the motors as an emergency
        measure. The motors would continue working if the connection to the
        base station is re-established.

        Raises
        ------
        MotorCountError
            If there are no motors registered
        NoSerialsError
            If there are no serial devices registered

        """
        if not self.motors:
            self.logger.critical("No motors registered!")
            raise MotorCountError(0)

        if not self.serials:
            self.logger.critical("No serial devices registered!")
            raise NoSerialsError

        self.logger.info("Client started")
        timed_out = False

        try:
            while True:
                try:
                    self.send("speeds")      # Request speed data.
                    result = self.request.recv(64)  # Receive speed data.
                except socket.timeout:
                    if not timed_out:
                        self.logger.warning("No connection to base station.")
                        self.logger.info("Turning off motors")
                        for motor in self.motors.values():
                            motor.drive(0)
                        timed_out = True
                    continue

                if timed_out:  # Connection returned.
                    timed_out = False

                # Get flipper positions from last two items of mbed reply.
                try:
                    sensor_data = self.serials["mbed"].readline().split()
                    *_, lpos, rpos = [int(i, 0) / 0xFFFF for i in sensor_data]
                    self.logger.debug("{:5.3f}  {:5.3f}".format(lpos, rpos))
                except ValueError:
                    self.logger.debug("An error occured when trying to read " +
                                      "the flipper positions from the mbed.")

                lmotor, rmotor, lflipper, rflipper = pickle.loads(result)
                self.motors["left_motor"].drive(lmotor)
                self.motors["right_motor"].drive(rmotor)

                # TODO(masasin): Hold position if input is 0.
                self.motors["left_flipper"].drive(lflipper)
                self.motors["right_flipper"].drive(rflipper)

                # Send sensor data back to base station.
                self._sensors_server.sendto(pickle.dumps(sensor_data),
                                            self.server_address)

        except (KeyboardInterrupt, RuntimeError, SystemExit):
            break

    def add_serial_device(self, name, ser):
        """
        Register a serial device for ADC communication.

        Parameters
        ----------
        name : str
            The name of the device.
        ser : Serial
            The serial connection to the microcontroller.

        """
        self.logger.debug("Registering {}".format(name))
        self.serials[name] = ser

    def add_motor(self, motor, ser=None, pwm_pins=None):
        """
        Set up and register a motor.

        This method must be called before ``run``. Either ``ser`` or
        ``pwm_pins`` must be provided.

        Parameters
        ----------
        motor : Motor
            The motor to be registered.
        ser : Serial, optional
            The serial connection to communicate with the microcontroller to
            which the motor drivers are connected. This is needed in order to
            enable hardware PWM.
        pwm_pins : 2-tuple of ints, optional
            The pins used for soft PWM. The elements are the PWM pin and the
            DIR pin, respectively.

        Raises
        ------
        NoDriversError
            Neither ``ser`` nor ``pwm_pins`` are provided.

        """
        self.logger.debug("Adding motor {}".format(motor))
        if not ser and not pwm_pins:
            self.logger.error("Cannot drive motor! No serial or PWM enabled.")
            raise NoDriversError(motor)

        if pwm_pins is not None:
            motor.enable_pwm(*pwm_pins)
        if ser is not None:
            motor.enable_serial(ser)

        self.motors[motor.name] = motor

    def remove_serial_device(self, name):
        """
        Deregister a serial device.

        Parameters
        ----------
        name : str
            The name of the device to be deregistered.

        """
        self.logger.debug("Removing {}".format(name))
        if name in self.serials:
            del self.serials[name]
        else:
            self.logger.warning("{} not found in serials".format(name))

    def remove_motor(self, motor):
        """
        Deregister a motor.

        Parameters
        ----------
        motor : Motor
            The motor to be deregistered.

        """
        self.logger.debug("Removing motor {}".format(motor))
        if motor.name in self.motors:
            del self.motors[motor.name]
        else:
            self.logger.warning("{} not found in motors".format(motor))

    def shutdown(self):
        """Shut down the client."""
        self.logger.debug("Shutting down client")
        self.request.close()
        self.logger.info("Client shut down")
