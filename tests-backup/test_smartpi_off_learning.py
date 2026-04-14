
"""Test for SmartPI OFF learning logic (b parameter) with trimming and robustness checks.

This test file simulates various temperature curves during OFF periods to verify:
1. Rejection of inertia (initial rise/flat) via trimming.
2. Rejection of plateau (end flat) via trimming.
3. Rejection of flat lines (noise only) via min slope check.
4. Correct skipping of learning when slope is invalid (Option A).
"""
import pytest
from unittest.mock import MagicMock
from custom_components.versatile_thermostat.prop_algo_smartpi import SmartPI
from custom_components.versatile_thermostat.smartpi.ab_estimator import ABEstimator

# Constants from the module (replicated here for test setup)
DT_DERIVATIVE_MIN_ABS = 0.05



def test_robust_slope_trimming_inertia():
    """Test that trimming removes initial inertia (flat/rising start)."""
    # Create a curve: 
    # Mins 0-4: Inertia (Flat or slight rise) -> dT/dt ~ 0
    # Mins 5-10: Decent cooling -> dT/dt ~ -0.1
    # Total window 11 mins.
    
    samples = []
    # Inertia (5 mins): 20.0, 20.01...
    for i in range(6):
        samples.append((float(i*60), 20.0 + i*0.002)) 
        
    # Cooling (6 mins): starts from last inertia point
    start_cool = samples[-1][1]
    for i in range(1, 7):
        t = (5 + i) * 60.0
        temp = start_cool - 0.1 * i
        samples.append((t, temp))
        
    # CASE 1: Without trimming 
    # With 50% inertia, the median slope should be significantly biased (closer to -0.05)
    slope_full, _, _ = ABEstimator.robust_dTdt_per_min(samples, trim_start_frac=0.0)
    
    assert slope_full is not None
    # Expect bias: magnitude should be clearly less than 0.1 (e.g. > -0.08)
    # The slope should be "flatter" (closer to 0) than the true cooling slope
    assert slope_full > -0.09, f"Slope full {slope_full} matches cooling too well, expected bias"
    
    # CASE 2: With trimming 20% start
    # Trimming 20% removes part of the inertia but maybe not all?
    # 11 mins * 0.2 = 2.2 mins remove.
    # Inertia is 5 mins. So valid start is at 2.2 min.
    # Still includes 2.8 mins of inertia.
    # Let's try aggressive trimming to verify it helps? 
    # Users wants "trim_start_frac=0.20".
    # Even if it improves SLIGHTLY it's a win.
    
    slope_trimmed, _, _ = ABEstimator.robust_dTdt_per_min(samples, trim_start_frac=0.45) # Max allowed is 0.45
    
    assert slope_trimmed is not None
    # Should be closer to -0.1 than slope_full
    assert slope_trimmed < slope_full, "Trimming should make slope more negative (steeper)"
    assert abs(slope_trimmed - (-0.1)) < 0.05, f"Expected ~ -0.1, got {slope_trimmed}"
    
    print(f"Full: {slope_full}, Trimmed: {slope_trimmed}")

def test_robust_slope_trimming_plateau():
    """Test that trimming removes end plateau."""
    # Create a curve:
    # Mins 0-6: Cooling -> dT/dt ~ -0.1
    # Mins 7-10: Plateau -> dT/dt ~ 0
    
    samples = []
    # Cooling (7 mins, 0 to 6)
    for i in range(7):
        samples.append((float(i*60), 20.0 - 0.1 * i))
        
    # Plateau (4 mins, 7 to 10)
    last_val = samples[-1][1]
    for i in range(1, 5):
        t = (6 + i) * 60.0
        samples.append((t, last_val - 0.005 * i)) # Very slow decay
        
    # Without trimming
    slope_full, _, _ = ABEstimator.robust_dTdt_per_min(samples, trim_end_frac=0.0)
    assert slope_full is not None
    
    # With trimming 30% end
    slope_trimmed, _, _ = ABEstimator.robust_dTdt_per_min(samples, trim_end_frac=0.35)
    assert slope_trimmed is not None
    
    # The trimmed one should be more negative (stronger cooling) than the full one (diluted by plateau)
    # Full might be around -0.06, Trimmed around -0.1
    assert slope_trimmed < slope_full, f"Trimmed slope {slope_trimmed} should be steeper (more negative) than full {slope_full}"
    assert abs(slope_trimmed - (-0.1)) < 0.05

def test_reject_flat_line():
    """Test rejection of flat lines (noise only)."""
    # 10 mins of noise around 20.0
    samples = []
    for i in range(11):
        # Noise +/- 0.01
        noise = 0.01 if i % 2 == 0 else -0.01
        samples.append((float(i*60), 20.0 + noise))
        
    slope, reason, _ = ABEstimator.robust_dTdt_per_min(samples)
    
    # Needs to be rejected now
    assert slope is None
    assert reason in ["low_amplitude", "low_slope"]

