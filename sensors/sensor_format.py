import re
import struct


def form(form, data):
    """
    Contains the formatting for raw measurements.

    Parameters
    ----------
    form : string
        Name of formatting method.
    data : byte array
        Data to be formatted.

    Returns
    -------
    result : string
        Formatted data.
    """
    result = -1
    form = form.casefold()
    print(f"Form: {form}")

    if form == "temp_ada":
        val = data[0] << 8 | data[1]
        result = (val & 0xFFF) / 16.0
        if val & 0x1000:
            result -= 256.0

    elif form == "atlas":
        # Byte 0 is the EZO status code, followed by the reading as ASCII.
        # Take the leading number: it stops at the NUL terminator, and at the
        # comma when a multi-output circuit (e.g. EC) sends several values,
        # of which the first is the primary reading.
        text = "".join(chr(x & ~0x80) for x in data[1:])
        match = re.match(r"[-+]?[0-9.]+", text)
        result = match.group() if match else ""

    elif form == "byte":
        result = struct.unpack("f", data)
        result = "".join(map(str, result))

    return result
