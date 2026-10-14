#!/usr/bin/env python3
"""Send one fixed test alert through SIM800L on Raspberry Pi."""

import re
import time
import serial

PORT = "/dev/serial0"
BAUD_RATES = (115200, 9600, 19200, 38400, 57600)

RECIPIENT = "+639083082262"

MESSAGE = (
    "ALERT\n"
    "Pig ID: 03\n"
    "Temperature: 41.8 C\n"
    "Behavior: Abnormal\n"
    "Timestamp: 2026-10-06 21:43:17"
)


def read_response(modem, timeout=3, prompt=False):
    response = ""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        chunk = modem.read(modem.in_waiting or 1)
        if not chunk:
            continue

        response += chunk.decode("ascii", errors="replace")
        lines = [
            line.strip()
            for line in response.replace("\r", "").split("\n")
        ]

        if "RDY" in lines:
            raise RuntimeError(
                "Modem restarted during the operation. "
                "Check power and connections."
            )

        if any(
            line == "ERROR"
            or line.startswith(("+CME ERROR:", "+CMS ERROR:"))
            for line in lines
        ):
            raise RuntimeError(response.strip())

        if prompt and ">" in response:
            return response

        if not prompt and "OK" in lines:
            return response

    raise TimeoutError(
        "Modem response timed out. Received: "
        + (response.strip() or "(nothing)")
    )


def command(modem, text, timeout=3, prompt=False):
    modem.reset_input_buffer()
    modem.write((text + "\r").encode("ascii"))
    modem.flush()
    return read_response(modem, timeout, prompt)


def connect(modem):
    for baud in BAUD_RATES:
        modem.baudrate = baud
        time.sleep(0.2)

        for _ in range(2):
            try:
                command(modem, "AT", timeout=1)
                command(modem, "ATE0")
                print(f"Modem responding at {baud} baud.")
                return
            except (TimeoutError, RuntimeError):
                pass

    raise RuntimeError(
        "No AT response. Check UART settings, TX/RX wiring, "
        "common ground, and modem power."
    )


def wait_for_network(modem):
    print("Waiting for network registration, up to 60 seconds...")
    deadline = time.monotonic() + 60
    last_status = "unknown"

    while time.monotonic() < deadline:
        response = command(modem, "AT+CREG?")
        match = re.search(r"\+CREG:\s*\d+\s*,\s*(\d+)", response)

        if match:
            last_status = match.group(1)

            if last_status in ("1", "5"):
                print("Registered on network.")
                return

            if last_status == "3":
                raise RuntimeError(
                    "Network registration denied. "
                    "Check SIM activation and operator service."
                )

        time.sleep(2)

    raise RuntimeError(
        f"Not registered. CREG status: {last_status}. "
        "Check antenna, SIM, coverage, and stable power."
    )


def validate_message(message):
    # Limit this test script to ASCII characters that map directly
    # to the GSM alphabet, including its extension characters.
    message.encode("ascii")
    unsupported = set(message) & set("`" + "\x1b")
    if unsupported or any(
        ord(char) < 32 and char not in "\r\n"
        for char in message
    ):
        raise ValueError("Message contains unsupported characters.")

    extension_characters = set("^{}\\[~]|")
    septets = sum(
        2 if char in extension_characters else 1
        for char in message
    )

    if septets > 160:
        raise ValueError("Message exceeds the single-SMS limit.")


def send_sms(modem, message, recipient):
    command(modem, "AT+CMGF=1")
    command(modem, 'AT+CSCS="GSM"')
    command(modem, "AT+CSMP=17,167,0,0")

    print(f"Sending one SMS to {recipient}...")

    try:
        command(
            modem,
            f'AT+CMGS="{recipient}"',
            timeout=10,
            prompt=True,
        )
    except Exception:
        modem.write(b"\x1b")
        modem.flush()
        raise

    try:
        modem.write(message.encode("ascii") + b"\x1a")
        modem.flush()
        response = read_response(modem, timeout=60)
    except (
        TimeoutError,
        serial.SerialException,
        OSError,
        RuntimeError,
    ) as error:
        raise RuntimeError(
            "SMS submission was not confirmed. Check the recipient's "
            "phone before retrying to avoid duplicates.\n"
            + str(error)
        ) from error

    if "+CMGS:" not in response:
        raise RuntimeError(
            "No SMS submission reference. "
            "Check the phone before retrying.\n"
            + response.strip()
        )

    print(response.strip())
    print("One SMS submitted. Check the recipient's phone.")

