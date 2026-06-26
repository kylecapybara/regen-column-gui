import serial
import time
from math import log10, floor

def to_scientific(number):
    # Puts number into scientific notation specified by reglo ICC (format = mmmmEse)
    # Example: 1.200 x 10-2 is represented with "1200E-2"
    exp = int(floor(log10(number)))
    pre = int(number*(10**-(exp-3)))
    return f"{pre:}{exp:+d}"

def from_scientific(string):
    # Opposite of to_scientific
    return float(string)/1e3

class reglo_ICC():
    # Control Codes
    CR = '\x0D'.encode() # carriage return
    LF = '\x0A'.encode() # line feed
    VB = '\x7C'.encode() # vertical bar a.k.a. pipe

    chs = [1,2,3,4]

    def __init__(self,COM_port,reply=True):
        # Initialization function for reglo_ICC object. Takes a string for the
        # port (e.g. "COM1"). "reply" sets whether responses and messages are
        # printed into the command line, default is "True".

        # Initializes the serial port, runs the setup, and gets the current
        # calibrations.

        self.reply = reply
        self.port = serial.Serial(
            port=COM_port,
            baudrate=9600,
            bytesize=8,
            stopbits=1,
            parity="N",
            timeout=1
            )
        try:
            self.handshake()
            self.setup()
            self.get_calibration()
        except Exception:
            self.port.close()
            raise

    def raw_command(self,code):
        # Function for sending a command (as string) to the pump. Encodes to binary
        # and adds CR, and then returns reponse

        self.port.reset_input_buffer()
        self.port.write(code.encode()+self.CR)
        response = self.port.readline().decode()
        if len(response) > 2:
            response = response[:-2]
        return response

    def handshake(self):
        # Protocol command 0! returns the serial protocol version, e.g. "2".
        response = self.raw_command("0!").strip()
        if not response or response == "#":
            raise ConnectionError("Reglo ICC did not respond to protocol handshake.")
        if not response.isdigit():
            raise ConnectionError(f"Unexpected Reglo ICC handshake response: {response!r}")
        self.protocol_version = response
        return response

    def setup(self,address=1):
        self.address = address
        # Sets the address for the pump, default 1
        self.raw_command(f"@{address:d}")
        # Configures independent channel control
        self.raw_command(f"{address:d}~1")

    def get_calibration(self):
        # Gets and stores the calibration value from each channel
        self.calibration = dict()
        for ch in self.chs:
            self.calibration[ch] = from_scientific(self.raw_command(f"{ch}r"))

    def set_calibration_one(self,channel,new_calibration):
        # Sets the calibration for a specific channel
        response = self.raw_command(f"{channel}r{to_scientific(new_calibration)}")
        if response == "#":
            print("Calibration set failed")
        else:
            self.calibration[channel] = new_calibration

    def set_calibration_all(self,data):
        # Sets the calibration for all channels, calibration data taken either
        # as a list of calibrations, or as a dictionary with channels as keys.
        if type(data) == dict:
            self.calibration = data
        elif type(data) == list:
            self.calibration = {ch: data[ch-1] for ch in self.chs}
        for ch in self.chs:
            self.raw_command(f"{ch}r{to_scientific(self.calibration[ch])}")

    def print_calibration(self):
        # Gets the current calibrations from the pump and prints the values
        self.get_calibration()
        print("Current Calibration:")
        for ch in self.chs:
            print(f" - Channel {ch}: {self.calibration[ch]:.3g} nL/step")

    def reset_calibration(self,which=[1,2,3,4]):
        # Resets calibration factors for channels specified by "which". Default
        # is all channels reset.
        for ch in which:
            self.raw_command(f"{ch}000000")
        if self.reply:
            print("Calibration data reset")

    def command_all(self,command,which=[1,2,3,4]):
        # Sends a command to multiple channels, specified by "which". Default
        # is all channels.
        responses = list()
        for ch in which:
            response = self.raw_command(f"{ch:d}{command}")
            responses.append(response)
            if self.reply:
                print(f" - Channel {ch}: ",end="")
                if response == "*":
                    print("command excecuted successfully")
                elif response == "#":
                    print("command NOT excecuted successfully")
                else:
                    print(response)

        return responses

if __name__ == "__main__":
    # For testing. Only run if this file is the main script being run.
    # This is NOT run when this file is imported as a module.

    reglo = reglo_ICC("COM6")
    #reglo.reset_calibration()
    reglo.print_calibration()

    if True:
        print("Setting all channels to Flow Rate mode")
        reglo.command_all("M")

        print("Setting all channels to 1.65 mm ID tubing")
        reglo.command_all("+0165")

        flowrate = 7.5
        print(f"Setting all channels to {flowrate:.2f} mL/min")

        reglo.command_all("f"+to_scientific(7.5))

        print("Setting all channels to run counter-clockwise")
        reglo.command_all("K")

        print("Maximum flow rate")
        reglo.command_all("?")

    print("Starting all pumps")
    reglo.command_all("H")
    time.sleep(60)
    print("Stopping all pumps")
    reglo.command_all("I")
