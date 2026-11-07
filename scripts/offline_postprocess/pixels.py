"""Bounded image analysis; DCGen-style separator evidence does not establish element GT."""
import numpy as np
from PIL import Image


def separation_lines(gray, policy=None):
    """Return DCGen-style blank/brightness separators in original-height analysis pixels.

    Adapted from scripts/layer_postprocess/pixel_regions.py:separation_lines; caller
    downsamples width only, preserving the original vertical coordinate system.
    """
    policy = policy or {}
    a = np.asarray(gray, dtype=np.float32)
    if a.ndim != 2 or min(a.shape) < 2:
        return []
    variance = policy.get('pixel_variance_threshold', 50)
    difference = policy.get('pixel_difference_threshold', 45)
    portion = policy.get('pixel_difference_portion', .9)
    window = max(2, int(policy.get('pixel_window_px', 24)))
    means, squares = a.mean(axis=1, dtype=np.float64), (a*a).mean(axis=1, dtype=np.float64)
    row_var = np.maximum(0, squares - means*means)
    m = np.r_[0, np.cumsum(means)]
    q = np.r_[0, np.cumsum(squares)]
    def block_var(lo, hi):
        return max(0, (q[hi]-q[lo])/(hi-lo)-((m[hi]-m[lo])/(hi-lo))**2)
    lines = []
    for end in range(2*window, len(a)-window):
        if block_var(end-window, end) < variance and (
                block_var(end-2*window, end-window) > variance or block_var(end, end+window) > variance):
            lines.append((int(end-window/2), 'soft_blank_band'))
    # Coalesce uniform plateaus, while keeping stable background transitions.
    previous = None
    for i in np.flatnonzero(row_var < variance):
        if previous is not None and np.mean(np.abs(a[i]-a[previous]) > difference) > portion:
            lines.append((int(i), 'hard_background_change'))
        previous = i
    return sorted(set(lines))


def inspect_image(path, width, height, policy):
    """Decode one PNG inside caller's semaphore; release image buffers before returning evidence."""
    with Image.open(path) as image:
        if image.size != (width, height):
            raise ValueError('capture_size_mismatch')
        if width * height > policy['max_decoded_pixels']:
            raise ValueError('image_decode_budget_exceeded')
        image.load()  # Verify actual compressed image data, not just PNG headers.
        with image.convert('L') as gray:
            with gray.resize((min(width, policy['analysis_width']), height)) as narrow:
                a = np.asarray(narrow, dtype=np.float32)
                std = float(a.std())
                edge = float(np.mean(np.abs(np.diff(a, axis=0)) > 12)) if height > 1 else 0.0
                lines = separation_lines(a, policy)
                analysis_gray = np.asarray(narrow, dtype=np.uint8).copy()
        with image.convert('RGB') as rgb:
            with rgb.resize((min(width, policy['rgb_analysis_width']), height), Image.Resampling.BILINEAR) as narrow_rgb:
                analysis_rgb = np.asarray(narrow_rgb, dtype=np.uint8).copy()
        return {'std': std, 'vertical_edge_ratio': edge, 'separators': lines,
                'analysis_gray': analysis_gray,
                'analysis_rgb': analysis_rgb,
                'semantic_error_detection': 'not_available_no_ocr_or_confirmed_template'}
