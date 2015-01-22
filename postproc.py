__author__ = 'aleaf'
'''

'''
import os
import numpy as np
import pandas as pd
import discomb_utilities as disutil
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import flopy

class SFRdata(object):

    def __init__(self, Mat1=None, Mat2=None, sfr=None, node_column=False,
                 mfpath=None, mfnam=None, mfdis=None, to_meters_mult=0.3048,
                 Mat2_out=None,
                 minimum_slope=1e-4):
        """
        Base class for SFR information in Mat1 and Mat2, or SFR package

        """
        if Mat1 is not None:
            # read Mats 1 and 2
            # allow the dataframes to be passed directly instead of reading them in from csvs
            if isinstance(Mat1, pd.DataFrame):
                self.m1 = Mat1
                self.m2 = Mat2
            else:
                self.MAT1 = Mat1
                self.MAT2 = Mat2
                self.m1 = pd.read_csv(Mat1).sort(['segment', 'reach'])
                self.m2 = pd.read_csv(Mat2).sort('segment')
            self.m2.index = self.m2.segment
        elif sfr is not None:
            self.read_sfr_package()
        else:
            # is this the right kind of error?
            raise AssertionError("Please specify either Mat1 and Mat2 files or an SFR package file.")

        self.elevs_by_cellnum = {} # dictionary to store the model top elevation by cellnumber

        if mfpath is not None:
            self.mfpath = mfpath
            self.mfnam = os.path.join(self.mfpath, mfnam)
            self.mfdis = os.path.join(self.mfpath, mfdis)

            # node numbers for cells with SFR
            if not node_column:
                self.node_column = 'node'
                self.gridtype = 'structured'
                self.read_dis2(self.mfdis)
                self.m1[self.node_column] = (self.dis.ncol * (self.m1['row'] - 1) + self.m1['column']).astype('int')
            else:
                self.node_column = node_column

        self.node_attribute = node_column # compatibility with sfr_plots and sfr_classes

        self.to_m = to_meters_mult # multiplier to convert model units to meters (used for width estimation)
        self.minimum_slope = minimum_slope

        self.segments = sorted(np.unique(self.m1.segment))
        self.Mat1_out = Mat1
        self.Mat2_out = Mat2_out

        # assign upstream segments to Mat2
        self.m2['upsegs'] = [self.m2.segment[self.m2.outseg == s].tolist() for s in self.segments]

        # check for circular routing
        c = [s for s in self.m2.segment if s == self.m2.outseg[s]]
        if len(c) > 0:
            raise ValueError('Warning! Circular routing in segments {}.\n'
                             'Fix manually in Mat2 before continuing'.format(', '.join(map(str, c))))

    def read_sfr_package(self):
        """method to read in SFR file
        """
        pass

    def read_dis2(self, mfdis):
        """read in model grid information using flopy
        """
        self.m = flopy.modflow.Modflow(model_ws=self.mfpath)
        self.nf = flopy.utils.mfreadnam.parsenamefile(self.mfnam, {})
        self.dis = flopy.modflow.ModflowDis.load(self.mfdis, self.m, self.nf)
        self.elevs = np.zeros((self.dis.nlay + 1, self.dis.nrow, self.dis.ncol))
        self.elevs[0, :, :] = self.dis.top.array
        self.elevs[0, :, :] = self.dis.top.array

        # check if there Quasi-3D confining beds
        if np.sum(self.dis.laycbd.array) > 0:
            print 'Quasi-3D layering found, skipping confining beds...'
            layer_ind = 0
            for l, laycbd in enumerate(self.dis.laycbd.array):
                self.elevs[l + 1, :, :] = self.dis.botm.array[layer_ind, :, :]
                if laycbd == 1:
                    print '\tbetween layers {} and {}'.format(l+1, l+2)
                    layer_ind += 1
                layer_ind += 1
        else:
            self.elevs[1:, :, :] = self.dis.botm.array

        # make dictionary of model top elevations by cellnum
        for c in range(self.dis.ncol):
            for r in range(self.dis.nrow):
                cellnum = r * self.dis.ncol + c + 1
                self.elevs_by_cellnum[cellnum] = self.elevs[0, r, c]

    def read_DIS(self, MFdis):
        """read discretisation file using disutil
        """
        self.DX, self.DY, self.NLAY, self.NROW, self.NCOL, i = disutil.read_meta_data(MFdis)

        # get layer tops/bottoms
        self.layer_elevs = np.zeros((self.NLAY+1, self.NROW, self.NCOL))
        for c in range(self.NLAY + 1):
            tmp, i = disutil.read_nrow_ncol_vals(MFdis, self.NROW, self.NCOL, 'float', i)
            self.layer_elevs[c, :, :] = tmp

        # make dictionary of model top elevations by cellnum
        self.elevs_by_cellnum = {}
        for c in range(self.NCOL):
            for r in range(self.NROW):
                cellnum = r*self.NCOL + c + 1
                self.elevs_by_cellnum[cellnum] = self.layer_elevs[0, r, c]

    def map_outsegs(self):
        '''
        from Mat2, returns dataframe of all downstream segments (will not work with circular routing!)
        '''
        self.outsegs = pd.DataFrame(self.m2.outseg)
        max_outseg = np.max(self.outsegs[self.outsegs.columns[-1]])
        knt = 2
        nsegs = len(self.m2)
        while max_outseg > 0:
            self.outsegs['outseg{}'.format(knt)] = [self.m2.outseg[s] if s > 0 and s < 999999 else 0
                                                    for s in self.outsegs[self.outsegs.columns[-1]]]
            max_outseg = np.max(self.outsegs[self.outsegs.columns[-1]])
            if max_outseg == 0:
                break
            knt +=1
            if knt > nsegs:
                print 'Circular routing encountered in segment {}'.format(max_outseg)
                break

    def consolidate_conductance(self, bedKmin=1e-8):
        """For model cells with multiple SFR reaches, shift all conductance to widest reach,
        by adjusting the length, and setting the lengths in all smaller collocated reaches to 1,
        and the K-values in these to bedKmin

        Parameters
        ----------

        bedKmin : float
            Hydraulic conductivity value to use for collocated SFR reaches in a model cell that are not the dominant
            (widest) reach. This is used to effectively set conductance in these reaches to 0, to avoid circulation
            of water between the collocated reaches.

        Returns
        -------
            Modifies the SFR Mat1 table dataframe attribute of the SFRdata object. A new SFR package file can be
            written from this table.

        Notes
        -----
            See the ConsolidateConductance notebook in the Notebooks folder.
        """
        # Calculate SFR conductance for each reach
        def cond(X):
            c = X['bed_K'] * X['width_in_cell'] * X['length_in_cell'] / X['bed_thickness']
            return c

        self.m1['Cond'] = self.m1.apply(cond, axis=1)

        shared_cells = np.unique(self.m1.ix[self.m1.node.duplicated(), 'node'])

        # make a new column that designates whether a reach is dominant in each cell
        # dominant reaches include those not collocated with other reaches, and the longest collocated reach
        self.m1['Dominant'] = [True] * len(self.m1)

        for c in shared_cells:

            # select the collocated reaches for this cell
            df = self.m1[self.m1.node == c].sort('width_in_cell', ascending=False)

            # set all of these reaches except the largest to not Dominant
            self.m1.loc[df.index[1:], 'Dominant'] = False

        # Sum up the conductances for all of the collocated reaches
        # returns a series of conductance sums by model cell, put these into a new column in Mat1
        Cond_sums = self.m1[['node', 'Cond']].groupby('node').agg('sum').Cond
        self.m1['Cond_sum'] = [Cond_sums[c] for c in self.m1.node]

        # Calculate a new length for widest reaches, set length in secondary collocated reaches to 1
        # also set the K values in the secondary cells to bedKmin
        self.m1['old_length_in_cell'] = self.m1.length_in_cell

        def consolidate_lengths(X):
            if X['Dominant']:
                lnew = X['Cond_sum'] * X['bed_thickness'] / (X['bed_K'] * X['width_in_cell'])
            else:
                lnew = 1.0
            return lnew

        self.m1['length_in_cell'] = self.m1.apply(consolidate_lengths, axis=1)
        self.m1['bed_K'] = [r['bed_K'] if r['Dominant'] else bedKmin for i, r in self.m1.iterrows()]


class Elevations(SFRdata):

    def __init__(self, Mat1=None, Mat2=None, sfr=None, node_column=False,
                 mfpath=None, mfnam=None, mfdis=None, to_meters_mult=0.3048,
                 minimum_slope=1e-4, landsurface=None, smoothing_iterations=0):
        """
        Smooth streambed elevations outside of the context of the objects in SFR classes
        (works off of information in Mat1 and Mat2; generates updated versions of these files
        """
        SFRdata.__init__(self, Mat1=Mat1, Mat2=Mat2, sfr=sfr, node_column=node_column,
                 mfpath=mfpath, mfnam=mfnam, mfdis=mfdis, to_meters_mult=to_meters_mult,
                 minimum_slope=minimum_slope)

        if landsurface:
            self.landsurface = np.fromfile(landsurface, sep=' ') # array of elevations to use (sorted by cellnumber)

            # assign land surface elevations based on node number
            self.m1['landsurface'] = [self.landsurface[n-1] for n in self.m1[self.node_column]]

        self.smoothing_iterations = smoothing_iterations

    def smooth_segment_ends(self, report_file='smooth_segment_ends.txt'):
        '''
        smooth segment end elevations so that they decrease monotonically down the stream network
        '''

        print '\nSmoothing segment ends...\n'
        # open a file to report a summary of the elevation adjustments
        self.ofp = open(report_file, 'w')
        self.ofp.write('Segment end smoothing report:\n'
                  'segment,max_elev,min_elev,downstream_min_elev\n')

        # set initial max / min elevations for segments based on max and min land surface elevations along each segment
        self.seg_maxmin = np.array([(np.max(self.m1.landsurface[self.m1.segment == s]),
                      np.min(self.m1.landsurface[self.m1.segment == s])) for s in self.segments])

        # determine constraining upstream min elevations for each segment in Mat2
        upstreamMin = np.array([np.min([self.seg_maxmin[useg-1][1] for useg in self.m2.ix[s, 'upsegs']])
                                  if len(self.m2.ix[s, 'upsegs']) > 0
                                  else self.seg_maxmin[s-1][0] for s in self.m2.index])

        # if the upstream minimum elevation is lower than the max elevation in the segment,
        # reset the max to the upstream minimum
        self.seg_maxmin = np.array([(upstreamMin[s-1], self.seg_maxmin[s-1][1])
                                    if upstreamMin[s-1] < self.seg_maxmin[s-1][0]
                                    else self.seg_maxmin[s-1] for s in self.segments])

        # check for higher minimum elevations in each segment
        bw = dict([(i, maxmin) for i, maxmin in enumerate(self.seg_maxmin) if maxmin[0] < maxmin[1]])

        # smooth any backwards elevations
        if len(bw) > 0:

            # creates dataframe of all outsegs for each segment
            self.map_outsegs()

            # iterate through the mapped outsegs, replacing minimum elevations until there are no violations
            self.fix_backwards_ends(bw)

        print 'segment ends smoothing finished in {} iterations.\n' \
              'segment ends saved to {}\n' \
              'See {} for report.'\
            .format(self.smoothing_iterations, self.MAT2[:-4] + '_elevs.csv', report_file)

        # populate Mat2 dataframe with bounding elevations for upstream and downstream segments, so they can be checked
        self.m2['upstreamMin'] = [np.min([self.seg_maxmin[useg-1][1] for useg in self.m2.ix[s, 'upsegs']])
                                  if len(self.m2.ix[s, 'upsegs']) > 0
                                  else self.seg_maxmin[s-1][0] for s in self.m2.index]
        self.m2['upstreamMax'] = [np.min([self.seg_maxmin[useg-1][0] for useg in self.m2.ix[s, 'upsegs']])
                                  if len(self.m2.ix[s, 'upsegs']) > 0
                                  else self.seg_maxmin[s-1][0] for s in self.m2.index]
        self.m2['Max'] = self.seg_maxmin[:, 0]
        self.m2['Min'] = self.seg_maxmin[:, 1]
        self.m2['downstreamMax'] = [self.seg_maxmin[s-1][0] for s in self.m2.outseg]
        self.m2['downstreamMin'] = [self.seg_maxmin[s-1][1] for s in self.m2.outseg]

        elevations_summary = self.m2[['segment','upstreamMax','upstreamMin','Max','Min','downstreamMax','downstreamMin']]
        elevations_summary.to_csv(self.ofp)
        self.ofp.close()

        # save new copy of Mat2 with segment end elevations
        self.m2 = self.m2.drop(['upstreamMax', 'upstreamMin', 'downstreamMax', 'downstreamMin', 'upsegs'], axis=1)
        self.m2.to_csv(self.MAT2[:-4] + '_elevs.csv', index=False)


    def replace_downstream(self, bw, level, ind):
        '''
        replace minimum elevations in segments with either the max or min elevation in downstream segment
        bw = dict with keys that are indices of segements to modify
        level = column of outsegs to reference in outsegs table
        ind = 0 (replace with downstream max elev) or 1 (min elev)
        get downstream max elevations for level from outsegs at that level (for segments with backwards elevs)
        '''
        # make list of downstream elevations (max or min, depending on ind)
        # if segment is an outlet, use minimum elevation of segment
        downstream_elevs = [self.seg_maxmin[self.outsegs.ix[s, level] - 1][ind]
                            if self.outsegs.ix[s, level] > 0
                            else np.min(self.seg_maxmin[s-1]) for s in self.segments]

        # assign any violating minimum elevations to downstream elevs from above
        bw_inds = bw.keys()
        #print 'segment {}: {}, downseg: {}'.format(bw_inds[0]+1, self.seg_maxmin[bw_inds[0]], downstream_elevs[bw_inds[0]])
        self.seg_maxmin[bw_inds] = np.array([(self.seg_maxmin[i][0], downstream_elevs[i]) for i in bw_inds])
        for i in bw_inds:
            self.ofp.write('{},{},{}\n'.format(i+1, self.seg_maxmin[i], downstream_elevs[i]))


    def replace_upstream(self):
        '''
        replace maximum elevations in segments with minimum elevations in upstream segments,
        in segments where the max is above the upstream minimum
        '''
        # make list of min upstream elevations
        # if segment is a headwater, use maximum elevation of segment
        upstreamMin = np.array([np.min([self.seg_maxmin[useg-1] for useg in self.m2.ix[s, 'upsegs']])
                       if len(self.m2.ix[s, 'upsegs']) > 0
                       else self.seg_maxmin[s-1][0] for s in self.segments])

        # if the upstream minimum elevation is lower than the max elevation in the segment,
        # reset the max to the upstream minimum
        self.seg_maxmin = np.array([(upstreamMin[s-1], self.seg_maxmin[s-1][1])
                                    if upstreamMin[s-1] < self.seg_maxmin[s-1][0]
                                    else self.seg_maxmin[s-1] for s in self.segments])


    def fix_backwards_ends(self, bw):

        knt = 1
        for level in self.outsegs.columns:

            # replace minimum elevations in backwards segments with downstream maximums
            self.replace_downstream(bw, level, 0)

            # check for higher minimum elevations in each segment
            bw = dict([(i, maxmin) for i, maxmin in enumerate(self.seg_maxmin) if maxmin[0] < maxmin[1]])

            # if still backwards elevations, try using downstream min elevations
            if len(bw) > 0:

                # replace minimum elevations in backwards segments with downstream minimums
                self.replace_downstream(bw, level, 1)

            # in segments where the max elevation is higher than the upstream minimum,
            # replace max elev with upstream minimum
            self.replace_upstream()

            # check again for higher minimum elevations in each segment
            bw = dict([(i, maxmin) for i, maxmin in enumerate(self.seg_maxmin) if maxmin[0] < maxmin[1]])

            # stop if there are no longer any backwards segments, otherwise continue
            if len(bw) == 0:
                break

            knt += 1

        self.smoothing_iterations = knt


    def interpolate(self, istart, istop):
        '''
        istart = index of previous minimum elevation
        istop = index of current minimum elevation
        dx = interpolation distance
        dS = change in streambed top over interpolation distance
        '''
        dx = self.cdist[istart] - self.cdist[istop]
        dS = self.minelev - self.elevs[istop]

        dist = 0

        if dS == 0:
            slope = 0
        else:
            slope = dS/dx

        for i in np.arange(istart+1, istop+1):

            dist += self.cdist[i-1] - self.cdist[i]
            self.sm.append(self.minelev - dist * slope)

            self.ofp.write('{:.0f},{:.0f},{:.2f},{:.2f},{:.2f},{:.2e},{:.2f}\n'
                           .format(self.seg, i, self.elevs[i-1], self.minelev, dist, slope, self.sm[-1]))

        # reset the minimum elevation to current
        self.minelev = self.sm[-1]


    def smooth_segment_interiors(self, report_file='smooth_segment_interiors.txt'):

        print '\nSmoothing segment interiors...\n'

        # open a file to report on interpolations
        self.ofp = open(report_file, 'w')
        self.ofp.write('segment, reach, land_surface, minelev, dist, slope, sb_elev\n')

        for seg in self.segments:
            self.seg = seg

            try:
                max, min = self.m2['Max'], self.m2['Min']
            except:
                raise IndexError("Max, Min elevation columns not found in Mat2. Call smooth_segment_ends method first")

            # make a view of the Mat 1 dataframe that only includes the segment
            df = self.m1[self.m1.segment == seg].sort('reach')

            # start with land surface elevations along segment
            self.elevs = df.landsurface.values


            # get start and end elevations from Mat2; if they are equal, continue
            start, end = self.m2.ix[seg, ['Max', 'Min']]
            if start == end:
                self.sm = [start] * len(self.elevs)
                self.m1.loc[self.m1.segment == seg, 'top_streambed'] = self.sm
                continue
            else:
                self.sm = [start]
                self.elevs[-1] = end

            # calculate cumulative distances at cell centers
            lengths = df.length_in_cell.values
            self.cdist = np.cumsum(lengths) - 0.5 * lengths

            self.minelev = start
            minloc = 0
            nreaches = len(self.elevs)

            for i in range(nreaches)[1:]:

                # if the current elevation is equal to or below the minimum
                if self.elevs[i] <= self.minelev:

                    # if the current elevation is above the end, interpolate from previous minimum to current
                    if self.elevs[i] >= end:
                        self.interpolate(minloc, i)
                        minloc = i

                    # otherwise if it is below the end, interpolate from previous minimum to end
                    elif self.elevs[i] <= end:

                        self.interpolate(minloc, nreaches-1)
                        break
                else:
                    continue

            # update Mat1 with smoothed streambed tops
            self.m1.loc[self.m1.segment == seg, 'top_streambed'] = self.sm

        # assign slopes to Mat 1 based on the smoothed elevations
        self.calculate_slopes()

        # save updated Mat1
        self.m1.to_csv(self.MAT1[:-4] + '_elevs.csv', index=False)
        self.ofp.close()
        print 'Done, updated Mat1 saved to {}.\nsee {} for report.'.format(self.MAT1[:-4] + '_elevs.csv', report_file)


    def calculate_slopes(self):
        '''
        assign a slope value for each stream cell based on streambed elevations
        this method is run at the end of smooth_segment_interiors, after the interior elevations have been assigned
        '''
        print 'calculating slopes...'
        for s in self.segments:

            # calculate the right-hand elevation differences (not perfect, but easy)
            diffs = self.m1[self.m1.segment == s].top_streambed.diff()[1:].tolist()

            if len(diffs) > 0:
                diffs.append(diffs[-1]) # use the left-hand difference for the last reach

            # edge case where segment only has 1 reach
            else:
                diffs = self.m2.Min[s] - self.m2.Max[s]

            # divide by length in cell; reverse sign so downstream = positive (as in SFR package)
            slopes = diffs / self.m1[self.m1.segment == s].length_in_cell * -1

            # assign to Mat1
            self.m1.loc[self.m1.segment == s, 'bed_slope'] = slopes
        
        # enforce minimum slope
        ns = [s if s > self.minimum_slope else self.minimum_slope for s in self.m1.bed_slope.tolist()]
        self.m1['bed_slope'] = ns


    def reset_model_top_2streambed(self, minimum_thickness=1,
                                   outdisfile=None, outsummary=None):
        """Make the model top elevation consistent with the SFR streambed elevations;
        Adjust other layers downward (this puts all SFR cells in layer1)

        Parameters
        ----------
        minimum_thickness : float
            Minimum layer thickness to enforce when adjusting underlying layers to accommodate
            changes to the model top

        outdisfile : string
            Name for new MODFLOW discretization file

        outsummary : string (optional)
            If specified, will save a summary (in SFR Mat1 style format)
            of adjustments made to the model top

        """
        if outdisfile is None:
            outdisfile = self.mfdis[:-4] + '_adjusted_to_streambed.dis'

        # output file summarizing adjustments made to model top
        if outsummary is None:
            outsummary = self.mfdis[:-4] + '_adjustments_to_model_top.csv'

        # make a vector of highest streambed values for each cell containing SFR (for collocated SFR cells)
        self.m1['highest_top'] = self.m1.top_streambed

        shared_cells = np.unique(self.m1.ix[self.m1.node.duplicated(), 'node'])
        for c in shared_cells:

            # select the collocated reaches for this cell
            df = self.m1[self.m1.node == c].sort('top_streambed', ascending=False)

            # set all of these reaches except the largest to not Dominant
            self.m1.loc[df.index, 'highest_top'] = np.max(df.top_streambed)

        # make a new model top array; assign highest streambed tops to it
        newtop = self.dis.top.array.copy()
        newtop[self.m1.row.values-1, self.m1.column.values-1] = self.m1.highest_top.values

        # Now straighten out the other layers, removing any negative thicknesses
        # do layer 1 first
        newbots = self.dis.botm.array.copy()
        conflicts = newbots[0, :, :] > newtop - minimum_thickness
        newbots[0, conflicts] = newtop[conflicts] - minimum_thickness

        for i in range(self.dis.nlay - 1):
            conflicts = newbots[i+1, :, :] > (newbots[i, :, :] - 1)
            newbots[i+1, conflicts] = newbots[i, conflicts] - minimum_thickness

        # make a dataframe that shows the largest adjustments made to model top
        adjustments = self.m1[self.m1.top_height > 0].sort(columns='top_height', ascending=False)
        adjustments.to_csv(outsummary)

        # write a new DIS file
        # (see Aberdeen work)
        new_m = flopy.modflow.mf.Modflow(modelname=outdisfile.split('.')[0])
        newdis = flopy.modflow.ModflowDis(new_m, nlay=self.dis.nlay, nrow=self.dis.nrow, ncol=self.dis.ncol,
                                          delr=self.dis.delr, delc=self.dis.delc, top=newtop, botm=newbots)
        newdis.write_file()

class Widths:

    def __init__(self, Mat1=None, Mat2=None, sfr=None, to_km=0.0003048, Mat2_out=None):

        SFRdata.__init__(self, Mat1=Mat1, Mat2=Mat2, sfr=sfr, to_meters_mult=to_km*1000, Mat2_out=Mat2_out)

        self.to_km = to_km # multiplier from model units to km

    def widthcorrelation(self, arbolate):
        #estimate widths, equation from Feinstein and others (Lake
        #Michigan Basin model) width=0.1193*(x^0.5032)
        # x=arbolate sum of stream upstream of the COMID in meters
        #NHDPlus has arbolate sum in kilometers.
        #print a table with reachcode, order, estimated width, Fcode
        estwidth = 0.1193 * (1000 * arbolate) ** 0.5032
        return estwidth


    def map_upsegs(self):
        '''
        from Mat2, returns dataframe of all upstream segments (will not work with circular routing!)
        '''
        # make a list of adjacent upsegments keyed to outseg list in Mat2
        upsegs = dict([(o, self.m2.segment[self.m2.outseg == o].tolist()) for o in np.unique(self.m2.outseg)])
        self.outsegs = [k for k in upsegs.keys() if k > 0] # exclude 0, which is the outlet designator

        # for each outseg key, for each upseg, check for more upsegs, append until headwaters has been reached
        for outseg in self.outsegs:

            up = True
            upsegslist = upsegs[outseg]
            while up:
                added_upsegs = []
                for us in upsegslist:
                    if us in self.outsegs:
                        added_upsegs += upsegs[us]
                if len(added_upsegs) == 0:
                    up = False
                    break
                else:
                    upsegslist = added_upsegs
                    upsegs[outseg] += added_upsegs

        # the above algorithm is recursive, so lower order streams get duplicated many times
        # use a set to get unique upsegs
        self.upsegs = dict([(u, list(set(upsegs[u]))) for u in self.outsegs])


    def estimate_from_arbolate(self):

        print 'estimating stream widths...'

        self.m2.in_arbolate = self.m2.in_arbolate.fillna(0) # replace any nan values with zeros

        # map upsegments for each outlet segment
        self.map_upsegs()

        # compute starting arbolate sum values for all segments (sum lengths of all mapped upsegs)
        asum_start = {}
        for oseg in self.outsegs:
            asum_start[oseg] = 0
            for us in self.upsegs[oseg]:
                # add the sum of all lengths for the upsegment
                asum_start[oseg] += self.m1.ix[self.m1.segment == us, 'length_in_cell'].sum() * self.to_km

                # add on any starting arbolate sum values from outside the model (will add zero if nothing was entered)
                asum_start[oseg] += self.m2.in_arbolate[us]
            #print 'outseg: {}, starting arbolate sum: {}'.format(oseg, asum_start[oseg])

        # assign the starting arbolate sum values to Mat2
        asum_starts = [asum_start[i] if i in self.outsegs
                       else self.m2.ix[i, 'in_arbolate'] for i, r in self.m2.iterrows()]
        self.m2['starting_arbolate'] = asum_starts

        for s in self.segments:

            segdata = self.m1[self.m1.segment == s] # shouldn't need to sort, as that was done in __init__

            # compute arbolate sum at each reach, in km, including starting values from upstream segments
            asums = segdata.length_in_cell.cumsum() * self.to_km + self.m2.ix[s, 'starting_arbolate']

            # compute width, assign to Mat1
            self.m1.loc[self.m1.segment == s, 'width_in_cell'] = [self.widthcorrelation(asum) for asum in asums]

        self.m1.to_csv(self.Mat1_out, index=False)
        print 'Done, updated Mat1 saved to {}.'.format(self.Mat1_out)

        if self.Mat2_out:
            self.m2.to_csv(self.Mat2_out, index=False)
            print 'saved arbolate sum information to {}.'.format(self.Mat2_out)


class Outsegs(SFRdata):

    def plot_outsegs(self, outpdf='complete_profiles.pdf', units='ft', add_profiles={}):
        print "plotting segment sequences..."
        # make a dataframe of all outsegs
        self.map_outsegs()

        # list of lists of connected segments
        seglists = [[p]+[s for s in self.outsegs.ix[p, :] if s > 0] for p in self.outsegs.index]

        # reduce seglists down to unique sets of segments
        unique_lists = []
        for l in seglists:
            # find all lists for which the current list is a subset
            overlap = np.where([set(l).issubset(set(ll)) for ll in seglists])
            # length 1 means that there are no other lists for which the current is a subset
            if len(overlap[0]) == 1:
                unique_lists.append(l)
            else:
                continue

        # add model top elevations to dictionary using node numbers
        self.m1['model_top'] = [self.elevs_by_cellnum[c] for c in self.m1[self.node_attribute]]

        pdf = PdfPages(outpdf)
        for l in unique_lists:
            print l
            if len(l) > 1:

                cdist = []
                cdist_end = 0
                cdist_ends = [0]
                sbtop = []
                modeltop = []
                self.elevs = {}

                for p in add_profiles.iterkeys():
                    self.elevs[p] = []

                # make each profile from its segments
                for s in l:
                    # make a view of the Mat 1 dataframe that only includes the segment
                    df = self.m1[self.m1.segment == s].sort('reach')

                    # calculate cumulative distances at cell centers
                    cdist += (np.cumsum(df['length_in_cell']) - 0.5 * df['length_in_cell'] + cdist_end).tolist()

                    cdist_end += np.sum(df['length_in_cell']) # add length of segment to total
                    #print cdist_end
                    cdist_ends.append(cdist_end) # record distances at segment boundaries

                    # streambed tops
                    sbtop += df['top_streambed'].tolist()

                    # model tops
                    modeltop += df['model_top'].tolist()

                    for p in add_profiles.iterkeys():
                        #elevs = self.cells2vertices(df[add_profiles[p]].tolist())
                        self.elevs[p] += df[add_profiles[p]].tolist()

                # plot the profile
                fig = plt.figure()
                ax = fig.add_subplot(111)
                plt.plot(cdist, modeltop, label='Model top', lw=0.5)
                plt.plot(cdist, sbtop, label='Streambed top')

                # add any additional profiles
                for p in add_profiles.iterkeys():
                    #print len(cdist)
                    #print len(self.elevs[p])
                    plt.plot(cdist, self.elevs[p], label=p, lw=0.5)

                # add in lines marking segment starts/ends
                # add in annotations labeling each segment
                for i, x in enumerate(cdist_ends[1:]):
                    plt.axvline(x, c='0.25', lw=0.5)

                    tx = 0.5 * (cdist_ends[i] + cdist_ends[i-1])
                    ty = ax.get_ylim()[0] + 0.9 * (ax.get_ylim()[1] - ax.get_ylim()[0])
                    ax.text(tx, ty, str(l[i-1]), ha='center', fontsize=8)

                ax.set_xlabel('Distance along SFR path')
                ax.set_ylabel('Elevation, {}'.format(units))
                title = 'SFR profile for segments:\n'
                for s in l:
                    title += ' {:.0f}'.format(s)
                ax.set_title(title, fontsize=10, fontweight='bold', loc='left')
                ax.set_ylim(int(np.floor(ax.get_ylim()[0])), int(np.ceil(ax.get_ylim()[1])))
                plt.legend(loc='best', fontsize=10)
                pdf.savefig()
                plt.close()
        pdf.close()

    def plot_routing(self, outpdf='routing.pdf'):

        # make a dataframe of all outsegs
        self.map_outsegs()

        # create new column in Mat2 listing outlets associated with each segment
        self.m2['Outlet'] = [r[r != 0][-1] if len(r[r != 0]) > 0
                             else i for i, r in self.outsegs.iterrows()]

        # assign the outlets to each reach listed in Mat1
        self.m1['Outlet'] = [self.m2.ix[seg, 'Outlet'] for seg in self.m1.segment]

        # make a matrix of the model grid, assign outlet values to SFR cells
        self.watersheds = np.empty((self.dis.nrow * self.dis.ncol))
        self.watersheds[:] = np.nan

        for s in self.segments:

            segdata = self.m1[self.m1.segment == s]
            cns = segdata[self.node_column].values
            self.watersheds[cns] = segdata.Outlet.values[0]

        self.watersheds = np.reshape(self.watersheds, (self.dis.nrow, self.dis.ncol))

        # now plot it up!
        self.watersheds[self.watersheds == 999999] = np.nan # screen out 999999 values
        self.routingfig = plt.figure()
        ax = self.routingfig.add_subplot(111)
        plt.imshow(self.watersheds)
        cb = plt.colorbar()
        cb.set_label('Outlet segment')
        plt.savefig(outpdf, dpi=300)

        self.m1.to_csv(self.MAT1, index=False)
        print 'Done, Mat1 updated with outlet information; saved to {}.'.format(self.MAT1)
