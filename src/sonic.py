### sonic.py ###
# Elliott Walker #
# Last update: 25 September 2024 #
# Analysis of the snippet of sonic data #

# Example usage:
#   python sonic.py -c -n 8 --data="../../data/KCC_FluxData_106m_SAMPLE/" --target="../outputs/sonic_sample/" --match="../outputs/slow/ten_minutes_labeled.csv" --slow="../outputs/slow/combined.csv"
#   This uses n=8 processors, clears target directory, and conducts a match with given slow data summary. Alignment by default.

# Note that summary file U, V, W means are pre-alignment, while those logged are post-alignment

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
from tqdm import tqdm
from datetime import datetime
import helper_functions as hf
import multiprocessing

WINDS = ['Ux','Uy','Uz'] # Columns containing wind speeds, in order
TEMPERATURE = 'Ts' # Column with sonic temperature for fluxes
TEMPS_C = ['Ts', 'amb_tmpr'] # Columns containing temperatures in C
IGNORE = ['H2O', 'CO2', 'amb_tmpr', 'amb_press'] # Columns we don't care about
COLORS = [f'C{i}' for i in range(7)]

class Logger:
    def __init__(self, logfile = 'output.log', pid = 0):
        self.is_printer = False
        self.is_void = False
        self.logfile = logfile
        self.pid = pid        
    
    def log(self, string, timestamp = False):
        log_string = f'[{datetime.now()}] {string}' if timestamp else str(string)
        pid = self.pid if self.pid else 'LOGPARENT'
        log_string = f'[[{pid}]] {log_string}'
        with open(self.logfile, 'a') as f:
            f.write(log_string+'\n')
        return

    def sublogger(self, pid = None):
        if pid is None:
            pid = os.getpid()
        self.log(f'Spawned sublogger for pid {pid}', timestamp=True)
        if self.is_printer:
            return Printer(pid = pid)
        if self.is_void:
            return VoidLogger()
        return Logger(logfile = self.logfile, pid = pid)
    
class VoidLogger(Logger):
    def __init__(self):
        Logger.__init__(self)
        self.is_void = True
    
    def log(self, string, timestamp = False):
        return
    
class Printer(Logger):
    def __init__(self, pid = 0):
        Logger.__init__(self, pid = pid)
        self.is_printer = True

    def log(self, string, timestamp = False):
        log_string = f'[{datetime.now()}] {string}' if timestamp else str(string) 
        if self.pid: log_string = f'[[{self.pid}]] {log_string}'
        print(log_string)
        return

# Loads dataframe: Handles timestamps, duplicate removal, column removal, and conversion.
def load_frame(filepath, # location of the CSV file to load
               kelvinconvert = TEMPS_C, # columns which should be converted from C -> K
               ignore = IGNORE
               ):

    df = pd.read_csv(filepath, low_memory = False).rename(columns={'TIMESTAMP' : 'time'})

    df['time'] = pd.to_datetime(df['time'], format = 'mixed')
    df.set_index('time', inplace = True)
    df = df[~df.index.duplicated(keep = 'first')]
    df.sort_index(inplace = True)

    for col in df.columns:

        if col in ignore: # We don't care about 
            df.drop(columns = [col], inplace = True)
            continue

        df[col] = pd.to_numeric(df[col], errors = 'coerce')

        if col in kelvinconvert: # Any column listed in kelvinconvert will have its values converted from C to K
            df[col] += 273.15

    return df

# Compute autocorrelations. Returns a dataframe of autocorrelations, timestamped by lag length.
def compute_autocorrs(df, # Dataframe to work with
                     autocols = [], # Columns to compute autocorrelations for
                     maxlag = 0.5, # Work for lags from 0 up to <maxlag> * <(duration of df)>
                     verbose = False,
                     logger = None
                     ):
    
    if autocols == []: # If an empty list is passed in, use all columns
        autocols = df.columns.tolist()

    kept = int(len(df)*maxlag)
    lost = len(df) - kept

    df_autocorr = pd.DataFrame(df.copy().reset_index()['time'][:kept])

    for col in autocols:

        lag_range = range(kept)

        if logger:
            logger.log(f'Autocorrelating for {col}', timestamp = True)

        if verbose or (logger and logger.is_printer):
            lag_range = tqdm(lag_range)

        Raa = []
        for lag in lag_range:
            autocorr = df[col].autocorr(lag = lag)
            Raa.append(autocorr)
        df_autocorr[f'R_{col}'] = Raa

    df_autocorr.set_index('time', inplace = True)
    df_autocorr.sort_index(inplace = True)
    starttime = df_autocorr.index[0]
    deltatime = df_autocorr.index - starttime
    df_autocorr['lag'] = deltatime.days * 24 * 3600 + deltatime.seconds + deltatime.microseconds/1e6
    df_autocorr.reset_index(drop = True)
    df_autocorr.set_index('lag', inplace = True)

    if logger:
        logger.log(f'Completed autocorrelations', timestamp = True)

    return df_autocorr

# Generate autocorrelation plots, and either save them to <saveto> or show them
def plot_autocorrs(df_autocorr,
                   title = 'Autocorrelation Plot',
                   saveto = None,
                   threshold=0.):
    
    fig, ax = plt.subplots()
    fig.suptitle(title, fontweight = 'bold')

    ax.plot(df_autocorr.index, [threshold]*len(df_autocorr), c='tab:gray', label = f'threshold = {threshold}', linewidth=1)
    if threshold != 0.:
        ax.plot(df_autocorr.index, [0.]*len(df_autocorr), c='black', linestyle='dashed', linewidth=1)

    for col in df_autocorr.columns:
        ax.plot(df_autocorr.index, df_autocorr[col], label = str(col)[2:], linewidth = 1)

    ax.set_ylim(-0.2,1.1)
    ax.set_ylabel('Autocorrelation')
    ax.set_xlabel('Lag (s)')
    ax.legend()

    fig.tight_layout(pad = 1)

    if saveto is None:
        plt.show()
    else:
        plt.savefig(saveto, bbox_inches='tight')
    plt.close()

    return

# Compute integral time and length scales
def integral_scales(df, # dataframe containing original data
                    df_autocorr, # dataframe containing the autocorrelations as computed by compute_autocorrs
                    cols = [], # wind speed column names in <df>
                    threshold = 0.25, # integrate up to the first time that the autocorrelation dips below this threshold
                    logger = None # Logger object for output
                    ):

    scales = dict()

    if cols == []:
        collist = df_autocorr.columns.tolist()
        for col in collist:
            cols.append(col[2:])

    warn = False
    for col in cols:

        Raa = df_autocorr[f'R_{col}']
        dt = df_autocorr.index[1] - df_autocorr.index[0]

        mean = df[col].mean()

        cutoff_index = 0
        for i, val in enumerate(Raa):
            if val < threshold:
                cutoff_index = i
                break

        if cutoff_index == 0:
            warn = True
            if logger:
                logger.log(f'Warning - failed to find cutoff for integration (variable {col}).')

        i_time = np.sum(Raa.loc[:cutoff_index]) * dt
        if i_time < 0:
            if logger:
                logger.log(f'Warning - found negative integral time scale (variable {col})')
        i_length = abs(i_time * mean)
        scales[col] = (i_time, i_length)

    return scales, warn

# Save information, including integral scales, to a text file
def save_scales(scales,
                filename,
                warn = False,
                bulk_ri = None,
                times = None,
                order = WINDS,
                align = False
                ):

    with open(filename, 'w') as f:

        if warn:
            f.write("Warning - at least one variable's autocorrelation did not fall below the threshold.")

        if align:
            f.write('Data geometrically aligned with Ux in direction of mean wind, Uy crosswind.\n')

        if times:
            f.write(times+'\n')
            
        if bulk_ri:
            f.write(bulk_ri+'\n')

        vars = order if set(order) == set(scales.keys()) else scales.keys()
        for var in vars:
            i_time, i_length = scales[var]
            mean = i_length/i_time
            f.write(f'{var}: Time scale = {i_time:.3f} s, Length scale = {i_length:.3f} m (Mean = {mean:.3f} m/s)\n')

    return

def slicematch(df, df_match):

    start_time = df.index[0]
    end_time = df.index[-1]

    dfr = df_match.reset_index()
    dfr['time'] = pd.to_datetime(dfr['time'])
    sliced = dfr[dfr['time'].between(start_time,end_time)]

    return sliced

# Match bulk Richardson number
def match_ri(df, # dataframe which we want to match ri to, based on its start & end times
             df_ri, # dataframe containing ri values
             where = 'ri' # Ri column name
             ):

    sliced = slicematch(df, df_ri)

    mean_ri = sliced[where].mean()
    median_ri = sliced[where].median()
    stability1 = hf.stability_class(mean_ri)
    stability2 = hf.stability_class(median_ri)
    stability = stability1 if stability1 == stability2 else f'{stability1}/{stability2}'

    return mean_ri, median_ri, stability

# Match computed alpha values
def match_alpha(df, # dataframe which we want to match alpha to, based on its start & end times
             df_alpha, # dataframe containing alpha values
             where = 'alpha' # wind shear exponent column name
             ):

    sliced = slicematch(df, df_alpha)

    mean_alpha = sliced[where].mean()
    median_alpha = sliced[where].median()

    return mean_alpha, median_alpha

# Match vpt lapse rate (vertical gradient of vpt, a static stability indicator)
def match_lapse(df,
                 df_lapse,
                 where = 'vpt_lapse_env'
                 ):
    
    sliced = slicematch(df, df_lapse)

    mean_lapse = sliced[where].mean()
    median_lapse = sliced[where].median()

    return mean_lapse, median_lapse

# Geometrically align the Ux and Uy components of wind such that Ux is oriented in the direction of the mean wind and Uy is in the crosswind direction
def mean_direction(df, components = WINDS[:2]):

    ux = df[components[0]]
    uy = df[components[1]]

    uxavg = np.mean(ux)
    uyavg = np.mean(uy)
    dir_to_align = np.arctan2(uyavg, uxavg)

    return dir_to_align

def align_to_direction(df, dir_to_align, components = WINDS[:2]):

    ux = df[components[0]]
    uy = df[components[1]]

    ux_aligned = ux * np.cos(dir_to_align) + uy * np.sin(dir_to_align)
    uy_aligned = - ux * np.sin(dir_to_align) + uy * np.cos(dir_to_align)

    dfc = df.copy()
    dfc[components[0]] = ux_aligned
    dfc[components[1]] = uy_aligned

    return dfc

def plot_data(df,
              title = 'Wind Plot',
              saveto = None,
              cols = WINDS,
              df_slow = None):
    
    fig, ax = plt.subplots()
    fig.suptitle(title, fontweight = 'bold')

    starttime = df.index[0]
    deltatime = df.index - starttime
    deltaseconds = hf.seconds(deltatime)

    if df_slow is not None:
        slowtime = df_slow.index - starttime
        slowseconds = hf.seconds(slowtime)

    for col, color in zip(cols, COLORS):
        if col not in df.columns:
            continue
        ax.plot(deltaseconds, df[col], label = str(col), linewidth = 1, c = color)
        if (df_slow is not None) and col in df_slow.columns:
            ax.scatter(slowseconds, df_slow[col], s = 20, c = color, edgecolors='black', zorder=10) # overlay slow data
    
    ax.set_ylabel('Wind speed (m/s)')
    ax.set_xlabel(f'Seconds since {starttime}')
    ax.legend()

    fig.tight_layout(pad = 1)

    if saveto is None:
        plt.show()
    else:
        plt.savefig(saveto, bbox_inches='tight')
    plt.close()

    return

def compute_fluxes(df, winds = WINDS, temp = TEMPERATURE):

    ucol, vcol, wcol = winds

    mean_u = np.mean(df[ucol])
    mean_v = np.mean(df[vcol])
    mean_w = np.mean(df[wcol])
    mean_T = np.mean(df[temp])

    flux_u = df[ucol] - mean_u
    flux_v = df[vcol] - mean_v
    flux_w = df[wcol] - mean_w
    flux_T = df[temp] - mean_T

    eddy_uMomt_flux = flux_w * flux_u
    eddy_vMomt_flux = flux_w * flux_v
    eddy_heat_flux = flux_w * flux_T

    dff = pd.DataFrame(data = {"w'u'" : eddy_uMomt_flux, "w'v'" : eddy_vMomt_flux, "w'T'" : eddy_heat_flux},
                       index = df.index,
                       copy = True)
    
    mean_eddy_uMomt_flux = np.mean(eddy_uMomt_flux)
    mean_eddy_vMomt_flux = np.mean(eddy_vMomt_flux)
    mean_eddy_heat_flux = np.mean(eddy_heat_flux)
    u_star = (mean_eddy_uMomt_flux**2 + mean_eddy_vMomt_flux**2)**(1/4)

    derived = dict()
    derived['Mean eddy u momentum flux'] = mean_eddy_uMomt_flux
    derived['Mean eddy v momentum flux'] = mean_eddy_vMomt_flux
    derived['Mean eddy heat flux'] = mean_eddy_heat_flux
    derived['Friction velocity'] = u_star
    derived['Obukhov length'] = hf.obukhov_length(u_star, mean_T, mean_eddy_heat_flux)
    derived['Flux Ri'] = hf.flux_richardson(mean_eddy_uMomt_flux, mean_T, mean_eddy_heat_flux, u_star)
    
    return dff, derived

def plot_flux(fluxes, title = 'Flux Plot', saveto = None):
    starttime = fluxes.index[0]
    deltatime = fluxes.index - starttime
    deltaseconds = deltatime.days * 24 * 3600 + deltatime.seconds + deltatime.microseconds/1e6

    fig, ax = plt.subplots(1, 1, sharex = True)
    fig.suptitle(title, fontweight = 'bold')

    for flux in fluxes.columns:
        ax.plot(deltaseconds, fluxes[flux], label = str(flux), linewidth=1)

    ax.set_ylabel('Flux')
    ax.set_xlabel('Seconds since {startime}')
    ax.legend()

    fig.tight_layout(pad = 1)

    if saveto is None:
        plt.show()
    else:
        plt.savefig(saveto, bbox_inches='tight')
    plt.close()

    return

def save_flux(derived, filename, bulk_ri = None, alpha = None):
    
    with open(filename, 'w') as f:

        if bulk_ri:
            f.write(bulk_ri+'\n')

        if alpha:
            f.write(alpha+'\n')
        
        for var, value in derived.items():
            f.write(f'{var}: {value:.4f}\n')
    
    return

def compute_rms(df, meanwind = "Ux"): # compute std of mean wind, which is RMS of its turbulent part, and return it alongside estimated TI (std/|mean|)

    mean = np.mean(df[meanwind])
    flux = df[meanwind] - mean
    squared = flux * flux
    rms = np.mean(squared) ** 0.5

    return rms, rms/np.abs(mean)

def compute_tke(df, winds = WINDS): # compute TKE using sonic data. Result is in m^2/s^2 = J/kg (assuming initial winds in m/s)

    total = 0
    for wind in winds:
        mean = np.mean(df[wind])
        flux = df[wind] - mean
        squared = flux * flux
        total += np.mean(squared)

    return total/2

def append_summary(info, filename):

    with open(filename, 'a') as f:
        f.write(f'{info}\n')

    return

def _analyze_file(args):
    filename, parent, kelvinconvert, autocols, maxlag, threshold, savedir, df_match, df_slow, align, savecopy, plotdata, plotautocorrs, saveautocorrs, savescales, plotflux, saveflux, summaryfile, logparent, multiproc = args

    if multiproc:
        logger = logparent.sublogger()
    else:
        logger = logparent

    path = os.path.abspath(os.path.join(parent, filename))
    if not(os.path.isfile(path) and filename[-4:] == '.csv'):
        return
    logger.log(f'Loading {path}', timestamp = True)

    name = filename[:-4]
    intermediate = f'{savedir}/{name}'
    os.makedirs(intermediate, exist_ok = True)
    
    df = load_frame(path, kelvinconvert = kelvinconvert)

    starttime = df.index[0]
    endtime = df.index[-1]
    time_string = f'Time interval: {starttime} to {endtime}'
    logger.log(time_string)

    if df_slow is not None:
        # cut to match time interval
        df_slow['time'] = pd.to_datetime(df_slow['time'])
        df_slow = df_slow[df_slow['time'].between(starttime, endtime)]
        df_slow.set_index('time', inplace = True)
        df_slow[['Ux','Uy']] = df_slow.apply(lambda row: hf.wind_components(row['ws_106m'], row['wd_106m'], invert=True), axis=1, result_type='expand') # convert to east and north components
        df_slow.drop(columns = ['ws_106m','wd_106m'], inplace=True)
        logger.log('Matched corresponding slow data at 106m')

    summaryinfo = f'{starttime},{endtime},{df[WINDS[0]].mean():.5f},{df[WINDS[1]].mean():.5f},{df[WINDS[2]].mean():.5f}'

    if align:
        dir_to_align = mean_direction(df) # determine direction of mean wind
        df = align_to_direction(df, dir_to_align) # align data to direction of mean wind
        logger.log('Aligned data: Ux oriented in direction of mean wind')
        if df_slow is not None:
            df_slow = align_to_direction(df_slow, dir_to_align) # also align the slow data to the same direction, if it exists
            logger.log('Aligned slow data to match orientation of sonic data')

    if savecopy: # if enabled, save a copy of the aligned filtered data we used
        if align:
            fname = 'aligned_data.csv'
        else:
            fname = 'data.csv'
        fpath = os.path.abspath(os.path.join(intermediate,fname))
        df.to_csv(fpath)
        logger.log(f'Copied data to {fpath}')

        if df_slow is not None: # also save a copy of the aligned filtered slow data, if it exists
            if align:
                fname = 'aligned_slowdata.csv'
            else:
                fname = 'slowdata.csv'
            fpath = os.path.abspath(os.path.join(intermediate,fname))
            df_slow.to_csv(fpath)
            logger.log(f'Copied matching slow data to {fpath}')

    if plotdata:
        if align:
            fname = 'aligned_data.png'
        else:
            fname = 'data.png'
        fpath = os.path.abspath(os.path.join(intermediate, fname))
        plot_data(df, title = f'{name} Data', saveto = fpath, cols = WINDS, df_slow = df_slow)
        logger.log(f'Saved wind plots to {fpath}')

    alpha_string = None
    ri_string = None
    if df_match is not None:
        mean_alpha, median_alpha = match_alpha(df, df_match)
        summaryinfo += f',{mean_alpha:.5f},{median_alpha:.5f}'
        alpha_string = f'Wind shear exponent alpha: mean {mean_alpha:.4f}, median {median_alpha:.4f}'
        logger.log(alpha_string)
        mean_ri, median_ri, stability = match_ri(df, df_match)
        summaryinfo += f',{mean_ri:.5f},{median_ri:.5f}'
        ri_string = f'Bulk Ri: mean {mean_ri:.4f}, median {median_ri:.4f} ({stability})'
        logger.log(ri_string)
        mean_lapse, median_lapse = match_lapse(df, df_match)
        summaryinfo += f',{mean_lapse:.5f},{median_lapse:.5f}'
        lapse_string = f'Envt VPT lapse rate: mean {mean_lapse:.4f}, median {median_lapse:.4f}'
        logger.log(lapse_string)

    rms, ti = compute_rms(df)
    if df_slow is not None:
        rms_slow, ti_slow = compute_rms(df_slow)
        summaryinfo += f',{rms:.5f},{rms_slow:.5f},{ti:.5f},{ti_slow:.5f}'
        logger.log(f'RMS: {rms:.4f} m/s (slow {rms_slow:.4f} m/s)')
        logger.log(f'TI: {ti:.4f} (slow {ti_slow:.4f})')
    else:
        summaryinfo += f',{rms:.5f},{ti:.5f}'
        logger.log(f'RMS: {rms:.4f} m/s')
        logger.log(f'TI: {ti:.4f}')

    tke = compute_tke(df)
    summaryinfo += f',{tke:.5f}'
    logger.log(f'Computed TKE: {tke:.4f} J/kg')

    df_autocorr = compute_autocorrs(df, autocols = autocols, maxlag = maxlag, logger = logger)

    if saveautocorrs:
        if align:
            fname = 'aligned_autocorrs.csv'
        else:
            fname = 'autocorrs.csv'
        fpath = os.path.abspath(os.path.join(intermediate,fname))
        df_autocorr.to_csv(fpath)
        logger.log(f'Saved autocorrelations to {fpath}')

    if plotautocorrs:
        if align:
            fname = 'aligned_autocorrs.png'
        else:
            fname = 'autocorrs.png'
        fpath = os.path.abspath(os.path.join(intermediate,fname))
        plot_autocorrs(df_autocorr, title = f'{name} Autocorrelations', saveto = fpath, threshold=threshold)
        logger.log(f'Saved autocorrelation plots to {fpath}')

    if plotflux or saveflux:
        fluxes, derived = compute_fluxes(df, winds = WINDS, temp = TEMPERATURE)
        logger.log(f'Computed flux information; flux Ri = {derived["Flux Ri"]}')

    if plotflux:
        fname = 'fluxes.png'
        fpath = os.path.abspath(os.path.join(intermediate, fname))
        plot_flux(fluxes, title = f'{name} Fluxes', saveto = fpath)
        logger.log(f'Saved flux plots to {fpath}')

    if saveflux:
        fname = 'flux_calculations.txt'
        fpath = os.path.abspath(os.path.join(intermediate, fname))
        save_flux(derived, filename = fpath, bulk_ri = ri_string, alpha = alpha_string)
        summaryinfo += f',{derived["Flux Ri"]:.5f},{derived["Obukhov length"]:.5f},{derived["Friction velocity"]:.5f}'
        logger.log(f'Saved flux information to {fpath}')

    if savescales:
        if align:
            fname = 'aligned_integralscales.txt'
        else:
            fname = 'integralscales.txt'
        scales, warn = integral_scales(df, df_autocorr, cols = list(set(WINDS)&set(autocols)), threshold = threshold, logger = logger)
        fpath = os.path.abspath(os.path.join(intermediate,fname))
        save_scales(scales, filename = fpath, warn = warn, bulk_ri = ri_string, times = time_string, align = align)
        length_scale = scales[WINDS[0]][1]
        summaryinfo += f',{length_scale:.5f}'
        logger.log(f'Saved info to {fpath}')

    for var, s in scales.items():
        logger.log(f'Mean {var} = {df[var].mean():.3f} m/s')
        i_time, i_length = s
        logger.log(f'\tIntegral time scale = {i_time:.3f} s')
        logger.log(f'\tIntegral length scale = {i_length:.3f} m')
    
    append_summary(summaryinfo, summaryfile)
    
def analyze_directory(parent, 
                      *,
                      kelvinconvert = TEMPS_C,
                      autocols = WINDS,
                      maxlag = 0.5,
                      threshold = 0.25,
                      savedir = '.',
                      matchfile = None,
                      slowfile = None,
                      align = True,
                      savecopy = True,
                      plotdata = True,
                      plotautocorrs = True,
                      plotflux = True,
                      saveautocorrs = True,
                      savescales = True,
                      saveflux = True,
                      summaryfile = None,
                      logger = Printer(),
                      nproc = 1
                      ):

    logger.log(f'Beginning analysis of {parent}', timestamp = True)

    if (summaryfile is not None) and align and saveflux  and (matchfile is not None):
        with open(summaryfile, 'w') as f:
            f.write('start,end,mean_u,mean_v,mean_w')
            if matchfile is not None:
                f.write(',alpha_mean,alpha_median,Rib_mean,Rib_median,lapse_mean,lapse_median')
            if slowfile is not None:
                f.write(',rms,slow_rms,ti,slow_ti,tke')
            else:
                f.write(',rms,ti,tke')
            if saveflux:
                f.write(',Rif,L,ustar')
            if savescales:
                f.write(',length_scale')
            f.write('\n')
        logger.log(f'Saving summary header information to {summaryfile}')

    if type(nproc) is int and nproc > 1:
        logger.log(f'MULTIPROCESSING ENABLED: {nproc=}')
        multiproc = True
    else:
        logger.log('Multiprocessing DISABLED.')
        nproc = 1
        multiproc = False
    
    if matchfile:
        df_match = pd.read_csv(matchfile)
        df_match.set_index('time', inplace = True)
    else:
        df_match = None

    if slowfile:
        df_slow = pd.read_csv(slowfile)
        df_slow = df_slow[['time','ws_106m','wd_106m']] # select only the 106m data from the slow file. this does for now require the specific formatting and data height for the slow data.
    else:
        df_slow = None

    arguments = (parent, kelvinconvert, autocols, maxlag, threshold, savedir, df_match, df_slow, align, savecopy, plotdata, plotautocorrs, saveautocorrs, savescales, plotflux, saveflux, summaryfile, logger, multiproc)
    directory = [(filename, *arguments) for filename in os.listdir(parent)]

    pool = multiprocessing.Pool(processes = nproc)
    
    # Use pool.map to distribute the work
    pool.map(_analyze_file, directory)
    pool.close()
    pool.join()

    logger.log(f'COMPLETED!', timestamp = True)

    return

def _confirm(message):
    response = input(message)
    if response.lower() == 'y':
        return True
    return False

if __name__ == '__main__':
    # Handle CL args

    import argparse

    parser = argparse.ArgumentParser(
        prog = 'sonic.py',
        description = 'Analyzes chunks of sonic data',
    )

    parser.add_argument('-c', '--clear', action = 'store_true', help = 'clear the target directory?')
    parser.add_argument('-y', '--yes', action = 'store_true', help = 'do not confirm before clearing?')
    parser.add_argument('-d', '--data', default = '../../data/KCC_FluxData_106m_SAMPLE', help = 'input data directory')
    parser.add_argument('-t', '--target', default = '../outputs/sonic_sample',  help = 'output target directory')
    parser.add_argument('-m', '--match', default = '../outputs/slow/ten_minutes_lapibeled.csv', help = 'file containing bulk Ri to match')
    parser.add_argument('-s', '--slow', default = '../outputs/slow/combined.csv', help = 'file containing slow data to match')
    parser.add_argument('--nomatch', action = 'store_true', help = 'do not perform Ri match?')
    parser.add_argument('--noslow', action = 'store_true', help = 'do not plot slow data?')
    parser.add_argument('--noflux', action = 'store_true', help = 'do not perform flux calculations?')
    parser.add_argument('--noalign', action = 'store_true', help = 'do not geometrically align Ux in the direction of the mean horizontal wind?')
    parser.add_argument('-n', '--nproc', default = 1, help = 'number of CPUs to run; sets verbose to False')
    parser.add_argument('-q', '--silent', action = 'store_true', help = 'neither print nor log?')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-v', '--verbose', action = 'store_true', help = 'print to standard output instead of logging?')
    group.add_argument('-l', '--logfile', help = 'file to log to')

    args = parser.parse_args()

    align = not args.noalign
    nomatch = args.nomatch
    noslow = args.noslow
    flux = not args.noflux
    parent = args.data
    savedir = args.target
    matchfile = args.match
    slowfile = args.slow

    if '/' not in savedir:
        savedir = f'./{savedir}'
    if '/' not in parent:
        parent = f'./{parent}'
    if '/' not in matchfile:
        matchfile = f'./{matchfile}'
    if '/' not in slowfile:
        slowfile = f'./{slowfile}'

    savedir = os.path.abspath(savedir)
    parent = os.path.abspath(parent)
    matchfile = os.path.abspath(matchfile)
    slowfile = os.path.abspath(slowfile)

    if not os.path.exists(parent):
        raise OSError(f'Data directory {parent} not found, exiting.')
    if not os.path.exists(matchfile):
        nomatch = True
    if not os.path.exists(slowfile):
        noslow = True

    verbose = args.verbose
    if int(args.nproc) > 1 or args.silent: verbose = False

    if nomatch:
        matchfile = None
    if noslow:
        slowfile = None

    if args.clear:
        if os.path.exists(savedir):
            if args.yes or _confirm(f'Really delete contents of {savedir}? (y/n): '):
                from shutil import rmtree
                rmtree(savedir)

    os.makedirs(savedir, exist_ok = True)

    if verbose:
        logger = Printer()
    elif args.silent:
        logger = VoidLogger()
    else:
        logfile = os.path.join(savedir, 'sonic_analysis.log')
        if args.logfile:
            logfile = args.logfile
            if '.' not in logfile:
                logfile += '.log'
            if '/' not in logfile:
                logfile = f'./{logfile}'
        logfile = os.path.abspath(logfile)
        logger = Logger(logfile = logfile)
    
    if flux and not align:
        flux = False
        logger.log('Warning - noalign is True, so flux calculations will not be carried out.')

    # Conduct the analysis with all of the options set
    analyze_directory(parent = parent,
                      maxlag = 0.5,
                      threshold = 0.5,
                      matchfile = matchfile,
                      slowfile = slowfile,
                      align = align,
                      savedir = savedir,
                      plotflux = flux,
                      saveflux = flux,
                      summaryfile = os.path.join(savedir, 'summary.csv'),
                      logger = logger,
                      nproc = int(args.nproc)
                    )
