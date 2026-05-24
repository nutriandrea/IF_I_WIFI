import numpy as np


class CsiRatioProcessor:
    """
    Processes CSI ratio H1/H2 for enhanced sensitivity to body movements.
    The ratio cancels shared environmental noise between antenna pairs.
    """

    def process(self, csi_data: np.ndarray) -> np.ndarray:
        """
        csi_data: (n_antennas, n_subcarriers) complex matrix
        returns: (n_antenna_pairs, n_subcarriers) ratio matrix
        """
        if csi_data.ndim == 1:
            return np.abs(csi_data).reshape(1, -1)
        n_ant, n_sub = csi_data.shape
        ratios = []
        for i in range(n_ant - 1):
            for j in range(i + 1, n_ant):
                with np.errstate(divide='ignore', invalid='ignore'):
                    ratio = np.where(np.abs(csi_data[j]) > 1e-10,
                                     csi_data[i] / csi_data[j], 0.0)
                ratios.append(np.abs(ratio))
        return np.array(ratios)


def conjugate_multiply(h_ref: np.ndarray, h_target: np.ndarray) -> np.ndarray:
    """
    Compute CSI ratio: h_ref * conj(h_target) for each subcarrier.
    Cancels hardware phase offsets (CFO, SFO, PDD).
    """
    if h_ref.shape != h_target.shape:
        raise ValueError(f"Shape mismatch: {h_ref.shape} vs {h_target.shape}")
    if h_ref.size == 0:
        raise ValueError("Empty input")
    return h_ref * np.conj(h_target)


def compute_ratio_matrix(csi_complex: np.ndarray) -> np.ndarray:
    """
    Compute CSI ratio matrix for all antenna pairs.

    Input: csi_complex (n_antennas, n_subcarriers) complex
    Output: (n_pairs, n_subcarriers) complex ratio matrix
    """
    if csi_complex.ndim != 2:
        raise ValueError(f"Expected 2D array, got shape {csi_complex.shape}")
    n_ant, n_sc = csi_complex.shape
    if n_ant < 2:
        raise ValueError(f"Insufficient antennas: {n_ant}")

    n_pairs = n_ant * (n_ant - 1) // 2
    ratio_matrix = np.zeros((n_pairs, n_sc), dtype=np.complex128)
    pair_idx = 0

    for i in range(n_ant):
        for j in range(i + 1, n_ant):
            ratio_matrix[pair_idx, :] = conjugate_multiply(csi_complex[i, :], csi_complex[j, :])
            pair_idx += 1

    return ratio_matrix
