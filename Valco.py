# Controller for VICI Valco Selector Valve
import re
import serial

class Valco():

    CR  = '\x0D'.encode()

    def __init__(self,COM_port):
        # Initialization function for Vici object

        self.COM_port = COM_port
        # Open the serial port connection
        self.port = serial.Serial(port=COM_port,
                                  baudrate=9600,
                                  parity=serial.PARITY_NONE,
                                  bytesize=serial.EIGHTBITS,
                                  stopbits=serial.STOPBITS_ONE,
                                  timeout=1
                                  )
        try:
            self.handshake()
        except Exception:
            self.port.close()
            raise

    def raw_command(self, command):
        self.port.reset_input_buffer()
        self.port.write(command.encode("ascii") + self.CR)
        return self.port.read_until(self.CR).decode("ascii", errors="replace").strip()

    def handshake(self):
        # CP displays the current position; AM displays the actuator mode.
        position_response = self.raw_command("CP")
        if not position_response:
            raise ConnectionError("Valco actuator did not respond to position handshake.")
        position = self._parse_position(position_response)

        mode_response = self.raw_command("AM")
        if not mode_response:
            raise ConnectionError("Valco actuator did not respond to mode handshake.")
        if not mode_response.startswith("AM"):
            raise ConnectionError(f"Unexpected Valco mode response: {mode_response!r}")

        self.current_position = position
        self.mode_response = mode_response
        return position_response

    def _parse_position(self, response):
        if response.startswith("E"):
            raise ConnectionError(f"Valco position handshake returned error: {response!r}")
        match = re.search(r"(\d+)\s*$", response)
        if not match:
            raise ConnectionError(f"Unexpected Valco position response: {response!r}")
        return int(match.group(1))

    def set_position(self,position,direction=None):
        # Set valve to given position. If direction is not specified, will default to shortest path

        if direction is None:
            if position%4 == (self.current_position-1)%4:
                # Move down to position
                command = f"CC{position:d}"
            else:
                # Move up to positin
                command = f"CW{position:d}"

        self.port.write(command.encode("ascii") + self.CR)
        self.current_position = position
        return

    def home(self):
        # Set valve to home (position 1)
        self.port.write("HM".encode("ascii") + self.CR)
        self.current_position = 1

# testing
if __name__ == "__main__":
    import time

    valve = Valco("COM5")
    valve.home()
    time.sleep(2)

    valve.set_position(3)
    time.sleep(2)

    valve.set_position(2)
    time.sleep(2)

    valve.set_position(1)
