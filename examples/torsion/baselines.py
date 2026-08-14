"""Baseline candidate sources for the torsion task.

BASELINE_CODE is the seed filter the proposer starts from (the naive
25-tap moving average). CANDIDATE_A_CODE is a hand-built reference point
(zero-phase 3.5-6.5 Hz bandpass) used ONLY in tests and the run.py
scoreboard — it is NEVER given to the proposer.
"""

BASELINE_CODE = '''\
import numpy as np

# torsion-filter kind=moving_average fc_low=0.0 fc_high=0.0 taps=25 window=none notch=0
def apply_filter(x, fs):
    k = np.ones(25) / 25.0
    return np.convolve(x, k, mode="same")
'''

CANDIDATE_A_CODE = '''\
import numpy as np

# torsion-filter kind=bandpass fc_low=3.5 fc_high=6.5 taps=801 window=hamming notch=0
def apply_filter(x, fs):
    m = np.arange(801) - 400.0
    h6 = np.sinc(2*6.5/fs * m) * np.hamming(801); h6 /= h6.sum()
    h3 = np.sinc(2*3.5/fs * m) * np.hamming(801); h3 /= h3.sum()
    k = h6 - h3
    y = np.convolve(x, k, mode="same")
    return np.convolve(y[::-1], k, mode="same")[::-1]
'''
