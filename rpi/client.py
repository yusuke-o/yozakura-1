# (C) 2015  Kyoto University Mechatronics Laboratory
# Released under the GNU General Public License, version 3
"""
Manage communication with the base station.

The client obtains motor speeds from the base station, and relays them to the
motors. If the received speed for a flipper is zero, it attempts to hold the
position based on encoder data received from the mbed.

The client also obtains the currents going to the motors, as well as total
system voltage and current use, by utilizing current sensors connected via I2C.
In addition, front and rear body pose is returned via I2C-connected IMUs.

All external sensor data is sent back to the base station via UDP.

"""
import logging
import pickle
import socket

from common.datatypes import CurrentSensorData, IMUData
from common.exceptions import BadDataError, NoDriversError, MotorCountError,\
    NoSerialsError


class Client(object):
    """
    A client to communicate with the base station and control the robot.

    Parameters
    ----------
    client_address : str
        The local IP address of Yozakura.
    server_address : 2-tuple of (str, int)
        The address at which the server is listening. The elements are the
        server IP address and the port number respectively.

    Attributes
    ----------
    request : socket
        Handles communication with the server.
    server_address : 2-tuple of (str, int)
        The address at which the server is listening. The elements are the
        server IP address and the port number respectively.
    motors : dict
        Contains all registered motors.

        **Dictionary format :** {name (str): motor (Motor)}
    serials : dict
        Contains all registered serial connections.

        **Dictionary format :** {name (str): connection (Serial)}
    current_sensors : dict
        Contains all registered current sensors.

        **Dictionary format :** {name (str): sensor (CurrentSensor)}
    imus : dict
        Contains all registered IMUs.

        **Dictionary format :** {name (str): imu (IMU)}

    """
    def __init__(self, client_address, server_address):
        self._logger = logging.getLogger("{ip}_client"
                                         .format(ip=client_address))
        self._logger.debug("Creating client")
        self.request = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.request.connect(server_address)
        self._logger.info("Connected to {server}:{port}"
                          .format(server=server_address[0],
                                  port=server_address[1]))

        self.request.settimeout(0.5)  # seconds

        self.server_address = server_address
        self._sensors_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.motors = {}
        self.serials = {}
        self.current_sensors = {}
        self.imus = {}

        self._timed_out = False

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
        self._logger.debug("Adding {name}".format(name=name))
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
        self._logger.debug("Adding {name}".format(name=motor))
        if not ser and not pwm_pins:
            raise NoDriversError(motor)

        if pwm_pins is not None:
            motor.enable_pwm(*pwm_pins)
        if ser is not None:
            motor.enable_serial(ser)

        self.motors[motor.name] = motor

    def add_current_sensor(self, sensor):
        """
        Register a current sensor.

        Parameters
        ----------
        sensor : CurrentSensor
            The current sensor associated with the motor.

        """
        self._logger.debug("Adding {name}".format(name=sensor))
        self.current_sensors[sensor.name] = sensor

    def add_imu(self, imu):
        """
        Register an IMU.

        Parameters
        ----------
        imu : IMU
            The IMU to be added.

        """
        self._logger.debug("Adding {name}".format(name=imu))
        self.imus[imu.name] = imu

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
            self._logger.critical("No motors registered!")
            raise MotorCountError(0)

        if not self.serials:
            self._logger.critical("No serial devices registered!")
            raise NoSerialsError

        self._logger.info("Client started")

        while True:
            try:
                speeds = self._request_speeds()
            except BadDataError as e:
                self._logger.debug(e)
                continue
            except socket.timeout:
                if not self._timed_out:
                    self._handle_timeout()
                continue
            except BrokenPipeError:
                self._logger.critical("Base station turned off!")
                raise SystemExit("Base station turned off!")

            if self._timed_out:
                self._logger.info("Connection returned")
                self._timed_out = False

            adc_data, positions = self._get_adc_data()
            self._drive_motors(speeds, positions)

            current_data = self._get_current_data("left_motor_current",
                                                  "right_motor_current",
                                                  "left_flipper_current",
                                                  "right_flipper_current",
                                                  "motor_current")

            imu_data = self._get_imu_data("front_imu", "rear_imu")

            self._send_data(positions, current_data, imu_data)

    def _handle_timeout(self):
        """Turn off motors in case of a lost connection."""
        self._logger.warning("Lost connection to base station")
        self._logger.info("Turning off motors")
        for motor in self.motors.values():
            motor.drive(0)
        self._timed_out = True

    def _request_speeds(self):
        """
        Request speed data from base station.

        Returns
        -------
        4-tuple of float
            A list of the speeds requested of each motor.

        """
        self.request.send(str.encode("speeds"))
        result = self.request.recv(64)
        if not result:
            raise BadDataError("No speed data")

        try:
            speeds = pickle.loads(result)
        except (pickle.UnpicklingError, EOFError):
            raise BadDataError("Invalid speed data")

        return speeds

    def _drive_motors(self, speeds, positions):
        """
        Drive all the motors.

        Parameters
        ----------
        speeds : 4-tuple of float
            The speeds with which to drive the four motors.
        positions : 2-tuple of float
            The current positions of the left and right flippers.

        """
        lwheel, rwheel, lflipper, rflipper = speeds
        lpos, rpos = positions

        self.motors["left_wheel_motor"].drive(lwheel)
        self.motors["right__wheel_motor"].drive(rwheel)

        # TODO(masasin): Hold position if input is 0.
        self.motors["left_flipper_motor"].drive(lflipper)
        self.motors["right_flipper_motor"].drive(rflipper)

    def _get_adc_data(self):
        """
        Get ADC data from the mbed.

        The mbed has six Analog-to-Digital Conversion ports, and polls all the
        ports that are currently active. The left and right flipper positions
        are always connected to the last two ports.

        Returns
        -------
        adc_data : list of float
            The ADC data from the mbed, or ``[]`` if invalid data was obtained.
        positions : list of float
            The flipper position data from the mbed, or ``[None, None]`` if
            invalid data was obtained.

        """
        try:
            mbed_data = self._serial_read_last("mbed").split()
            # mbed_data = self.serials["mbed"].readline().split()
            float_data = [int(i, 16) / 0xFFFF for i in mbed_data]
        except ValueError:
            self._logger.debug("Bad mbed flipper data")
            float_data = [None, None]

        adc_data = float_data[:-2]
        positions = float_data[-2:]

        return adc_data, positions

    def _serial_read_last(self, name):
        """
        Read the last line from a serial device's buffer.

        Parameters
        ----------
        name : str
            The name of the serial device to be read.

        Returns
        -------
        str
            The last line from the serial device's buffer.

        """
        buffer_string = ""
        dev = self.serials[name]
        while True:
            buffer_string += dev.read(dev.inWaiting()).decode()
            if "\n" in buffer_string:
                return buffer_string.split("\n")[-2]

    def _get_current_data(self, current_sensors):
        """
        Get data from the requested current sensors.

        Parameters
        ----------
        current_sensors : list of str
            A list containing the names of the current sensors requested.

        Returns
        -------
        current_data : list of CurrentSensorData
            A list containing the current, power, and voltage readings of each
            sensor. If invalid sensor data was obtained, the readings are set
            to ``[None, None, None]`` by default.

        """
        current_data = []
        for sensor in current_sensors:
            try:
                current_data.append(self.current_sensors[sensor].ipv)
            except KeyError:
                self._logger.debug("{sensor} not registered".format(
                    sensor=sensor))
                current_data.append(CurrentSensorData([None, None, None]))
        return current_data

    def _get_imu_data(self, imus):
        """
        Get data from the requested inertial measurement units.

        Parameters
        ----------
        imus : list of str
            A list containing the names of the IMUs requested.

        Returns
        -------
        imu_data : list of IMUData
            A list containing the roll, pitch, and yaw readings of each sensor.
            If invalid sensor data was obtained, the readings are set to
            ``[None, None, None]`` by default.

        """
        imu_data = []
        for imu in imus:
            try:
                imu_data.append(self.imus[imu].rpy)
            except KeyError:
                self._logger.debug("{imu} not registered".format(imu=imu))
                imu_data.append(IMUData([None, None, None]))
        return imu_data

    def _send_data(self, positions, current_data, imu_data, protocol=2):
        """
        Send data via UDP.

        Parameters
        ----------
        positions : list of float
            The positions of the flippers.
        current_data : list of CurrentSensorData
            The current sensor measurements.
        imu_data : list of IMUData
            The IMU measurements.
        protocol : int, optional
            The protocol to use to pickle the data. The ROS-based base station
            software uses Python 2, and therefore the maximum usable protocol
            version is 2. If you are sure that Python 2 will not be
            used, feel free to use whatever protocol verison is necessary.

        """
        self._sensors_server.sendto(pickle.dumps((positions,
                                                  current_data,
                                                  imu_data),
                                                 protocol=protocol),
                                    self.server_address)

    def shutdown(self):
        """Shut down the client."""
        self._logger.debug("Shutting down client")
        self.request.close()
        self._logger.info("Client shut down")
