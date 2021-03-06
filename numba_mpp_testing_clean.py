from time import clock

import numpy as np
from numba import jit, njit, vectorize, float64
import pandas as pd
import scipy.optimize

import pvlib
from pvlib import pvsystem


@njit
def bishop88_jit(vd, photocurrent, saturation_current, resistance_series,
                 resistance_shunt, nNsVth):
    """
    Explicit calculation single-diode-model (SDM) currents and voltages using
    diode junction voltages [1].
    [1] "Computer simulation of the effects of electrical mismatches in
    photovoltaic cell interconnection circuits" JW Bishop, Solar Cell (1988)
    https://doi.org/10.1016/0379-6787(88)90059-2
    :param numeric vd: diode voltages [V]
    :param numeric photocurrent: photo-generated current [A]
    :param numeric saturation_current: diode one reverse saturation current [A]
    :param numeric resistance_series: series resitance [ohms]
    :param numeric resistance_shunt: shunt resitance [ohms]
    :param numeric nNsVth: product of thermal voltage ``Vth`` [V], diode
        ideality factor ``n``, and number of series cells ``Ns``

    :returns: tuple containing currents [A], voltages [V], power [W],
    """
    a = np.exp(vd / nNsVth)
    b = 1.0 / resistance_shunt
    i = photocurrent - saturation_current * (a - 1.0) - vd * b
    v = vd - i * resistance_series
    retval = i, v, i*v
    return retval


@njit([float64(float64, float64, float64, float64, float64, float64)])
def bishop88_gradp_jit(vd, photocurrent, saturation_current, resistance_series,
             resistance_shunt, nNsVth):
    """root finders only need dp/dv"""
    a = np.exp(vd / nNsVth)
    b = 1.0 / resistance_shunt
    i = photocurrent - saturation_current * (a - 1.0) - vd * b
    v = vd - i * resistance_series
    c = saturation_current * a / nNsVth
    grad_i = - c - b  # di/dvd
    grad_v = 1.0 - grad_i * resistance_series  # dv/dvd
    # dp/dv = d(iv)/dv = v * di/dv + i
    grad = grad_i / grad_v  # di/dv
    grad_p = v * grad + i  # dp/dv
    return grad_p


@njit
def est_voc_jit(photocurrent, saturation_current, nNsVth):
    """
    Rough estimate of open circuit voltage useful for bounding searches for
    ``i`` of ``v`` when using :func:`~pvlib.pvsystem.singlediode`.
    :param numeric photocurrent: photo-generated current [A]
    :param numeric saturation_current: diode one reverse saturation current [A]
    :param numeric nNsVth: product of thermal voltage ``Vth`` [V], diode
        ideality factor ``n``, and number of series cells ``Ns``
    :returns: rough estimate of open circuit voltage [V]
    Calculating the open circuit voltage, :math:`V_{oc}`, of an ideal device
    with infinite shunt resistance, :math:`R_{sh} \\to \\infty`, and zero series
    resistance, :math:`R_s = 0`, yields the following equation [1]. As an
    estimate of :math:`V_{oc}` it is useful as an upper bound for the bisection
    method.
    .. math::
        V_{oc, est}=n Ns V_{th} \\log \\left( \\frac{I_L}{I_0} + 1 \\right)
    [1] http://www.pveducation.org/pvcdrom/open-circuit-voltage
    """
    return nNsVth * np.log(photocurrent / saturation_current + 1.0)


@vectorize([float64(float64, float64, float64, float64, float64, float64)], target='cpu')
def slow_vd_jit_vec(photocurrent, saturation_current, resistance_series,
                    resistance_shunt, nNsVth, voc_est):
    """
    This is a slow but reliable way to find mpp.
    """
    # collect args
    args = (photocurrent, saturation_current, resistance_series,
            resistance_shunt, nNsVth)
    # first bound the search using voc
    vd = scipy.optimize.brentq(bishop88_gradp_jit, 0.0, voc_est, args)
    return vd


@jit([float64(float64, float64, float64, float64, float64)])
def slow_mpp_jit(photocurrent, saturation_current, resistance_series,
                 resistance_shunt, nNsVth):
    voc_est = est_voc_jit(photocurrent, saturation_current, nNsVth)
    # root finder fails if bounds are both 0
    nonzeros = voc_est != 0
    IL_pos = photocurrent[nonzeros]
    RSH_pos = resistance_shunt[nonzeros]
    voc_est_pos = voc_est[nonzeros]
    vd_pos = slow_vd_jit_vec(IL_pos, saturation_current, resistance_series,
                             RSH_pos, nNsVth, voc_est_pos)
    vd = np.zeros_like(photocurrent)
    vd[nonzeros] = vd_pos
    mpp = bishop88_jit(vd, photocurrent, saturation_current, resistance_series,
                       resistance_shunt, nNsVth)
    # guessing that some code is needed here to handle nans and/or
    # differences between pandas/numpy nonzeros indexing
    return mpp


def prepare_data():
    print('preparing single diode data from clear sky ghi...')
    # adjust values to change length of test data
    times = pd.DatetimeIndex(start='20180101', end='20190101', freq='1min', tz='America/Phoenix')
    location = pvlib.location.Location(32.2, -110.9, altitude=710)
    cs = location.get_clearsky(times)
    poa_data = cs['ghi']
    cec_modules = pvsystem.retrieve_sam('cecmod')
    cec_module_params = cec_modules['Example_Module']
    IL, I0, Rs, Rsh, nNsVth = pvsystem.calcparams_desoto(
                                     poa_data,
                                     temp_cell=25,
                                     alpha_isc=cec_module_params['alpha_sc'],
                                     module_parameters=cec_module_params,
                                     EgRef=1.121,
                                     dEgdT=-0.0002677)
    return IL, I0, Rs, Rsh, nNsVth


if __name__ == '__main__':
    IL, I0, Rs, Rsh, nNsVth = prepare_data()

    print('number of points = %s' % len(IL))

    for n in range(4):
        tstart = clock()
        singlediode_out = pvsystem.singlediode(IL, I0, Rs, Rsh, nNsVth)
        tstop = clock()
        dt_slow = tstop - tstart
        print('%s singlediode elapsed time = %g[s]' % (n, dt_slow))

    for n in range(4):
        tstart = clock()
        i_mp, v_mp, p_mp = slow_mpp_jit(IL.values, I0, Rs, Rsh.values, nNsVth)
        i_mp = pd.Series(i_mp, index=IL.index)
        v_mp = pd.Series(v_mp, index=IL.index)
        p_mp = pd.Series(p_mp, index=IL.index)
        tstop = clock()
        dt_slow = tstop - tstart
        print('%s slow_mpp_jit elapsed time = %g[s]' % (n, dt_slow))

    print('(singlediode - slow_mpp_jit).abs()\n',
          (singlediode_out['p_mp'] - p_mp.fillna(0)).describe())
