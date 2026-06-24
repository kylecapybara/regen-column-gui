# Controller for VICI Valco Selector Valve
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
        
        # Ensure that the valve is starting at 1
        self.home()
        self.current_position = 1

    def set_position(self,position,direction=None):
        # Set valve to given position. If direction is not specified, will default to shortest path

        if direction is None:
            if position%4 == (self.current_position-1)%4:
                # Move down to position
                command = f"CC{position:d}"
            else:
                # Move up to positin
                command = f"CW{position:d}"
        
        self.current_position = position
        self.port.write(command.encode("ascii") + self.CR)
        return
    
    def home(self):
        # Set valve to home (position 1)
        self.port.write("HM".encode("ascii") + self.CR)

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