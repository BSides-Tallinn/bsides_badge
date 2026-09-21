"""Battery measurement helpers for the 2026 badge."""

BATTERY_DIVIDER_RATIO = 6.0  # 100k high side, 20k low side

# Rough LiPo resting-voltage curve. Load and charging state affect the result.
SOC_CURVE = (
    (3.00, 0),
    (3.30, 5),
    (3.50, 10),
    (3.60, 20),
    (3.70, 40),
    (3.80, 60),
    (3.90, 75),
    (4.00, 85),
    (4.10, 95),
    (4.20, 100),
)


def estimate_soc(voltage):
    """Return an approximate 0-100% LiPo state of charge."""
    if voltage <= SOC_CURVE[0][0]:
        return 0
    for index in range(1, len(SOC_CURVE)):
        low_v, low_pct = SOC_CURVE[index - 1]
        high_v, high_pct = SOC_CURVE[index]
        if voltage <= high_v:
            fraction = (voltage - low_v) / (high_v - low_v)
            return int(low_pct + fraction * (high_pct - low_pct) + 0.5)
    return 100


def read_battery_voltage(pin_number, samples=8):
    """Read VBAT through the 2026 badge's GPIO4 voltage divider."""
    from machine import ADC, Pin

    adc = ADC(Pin(pin_number))
    try:
        adc.atten(ADC.ATTN_0DB)
    except AttributeError:
        pass

    if hasattr(adc, "read_uv"):
        total_uv = 0
        for _ in range(samples):
            total_uv += adc.read_uv()
        return total_uv * BATTERY_DIVIDER_RATIO / samples / 1000000

    # ESP32-C3 0 dB nominal full scale. read_uv() is preferred because it uses
    # calibration data when the MicroPython build provides it.
    total = 0
    for _ in range(samples):
        total += adc.read_u16()
    return total * 0.95 * BATTERY_DIVIDER_RATIO / samples / 65535
