import math

SPEED_OF_LIGHT = 2.998e8


class FresnelError(Exception):
    """Fresnel zone calculation errors."""
    pass


class FresnelGeometry:
    """
    Models WiFi signal variation from chest displacement at Fresnel zone boundaries.

    At 5 GHz (λ=60mm), chest displacement of 5-10mm during breathing
    is a significant fraction of Fresnel zone width.
    """

    def __init__(self, d_tx_body: float, d_body_rx: float, frequency: float = 5.8e9):
        """d_tx_body, d_body_rx: distances in meters. frequency in Hz."""
        if d_tx_body <= 0 or d_body_rx <= 0:
            raise FresnelError("Distances must be positive")
        if frequency <= 0:
            raise FresnelError("Frequency must be positive")
        self.d_tx_body = d_tx_body
        self.d_body_rx = d_body_rx
        self.frequency = frequency

    def wavelength(self) -> float:
        return SPEED_OF_LIGHT / self.frequency

    def fresnel_radius(self, n: int) -> float:
        """Radius of nth Fresnel zone at the body point."""
        lam = self.wavelength()
        return math.sqrt(n * lam * self.d_tx_body * self.d_body_rx /
                        (self.d_tx_body + self.d_body_rx))

    def phase_change(self, displacement_m: float) -> float:
        """Phase change from chest displacement: ΔΦ = 2π * 2Δd / λ"""
        return 2 * math.pi * 2 * displacement_m / self.wavelength()

    def expected_amplitude_variation(self, displacement_m: float) -> float:
        """|sin(ΔΦ/2)| when reflection crosses Fresnel boundaries."""
        return abs(math.sin(self.phase_change(displacement_m) / 2))

    def fresnel_zone_at_position(self, pos, tx_pos, rx_pos) -> int:
        """Determine which Fresnel zone contains the body point."""
        d1 = math.dist(pos, tx_pos)
        d2 = math.dist(pos, rx_pos)
        d_direct = math.dist(tx_pos, rx_pos)
        path_diff = (d1 + d2) - d_direct
        return int(path_diff / (self.wavelength() / 2))

    def is_in_fresnel_zone(self, pos, tx_pos, rx_pos, n: int, tolerance: float = 0.1) -> bool:
        """Check if point is near nth Fresnel zone boundary."""
        actual = self.fresnel_zone_at_position(pos, tx_pos, rx_pos)
        return abs(actual - n) <= tolerance
