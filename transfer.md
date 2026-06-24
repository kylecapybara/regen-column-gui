Here’s the clean handoff process for another computer with Anaconda installed:

1. Copy the project folder to the other machine, including app.py, reglo_ICC.py, Valco.py, templates, static, and requirements.txt.

2. Create and activate a conda environment on that machine:
```bash
conda create -n reglo-gui python=3.12 -y
conda activate reglo-gui
```

3. Install the Python dependencies:
```bash
pip install -r requirements.txt
```

4. Start the app from the project folder:
```bash
python app.py
```

5. Open the browser on that computer:
```text
http://127.0.0.1:5001/
```

If you want to open it from a different computer on the same network, use the host machine’s LAN IP instead of localhost, for example:
```text
http://192.168.x.x:5001/
```

A couple of practical notes:
- The current startup port is set in app.py. Right now it runs on port `5001`.
- If the second computer is the one connected to the pump/valve hardware, it also needs the correct USB/serial drivers and access to the COM/tty ports.
- If Windows or macOS firewall prompts appear, allow Python/Anaconda to accept local network connections.

If you want, I can also make a short `README.md` with these exact deployment steps baked into the repo.