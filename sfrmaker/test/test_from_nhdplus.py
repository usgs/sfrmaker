import os
import shutil
import numpy as np
import pytest
import flopy.modflow as fm
import sfrmaker
from sfrmaker.checks import reach_elevations_decrease_downstream

#TODO: make tests more rigorous

@pytest.fixture(scope='module')
def lines_from_NHDPlus(datapath):
    pfvaa_files = ['{}/badriver/PlusFlowlineVAA.dbf'.format(datapath)]
    plusflow_files = ['{}/badriver/PlusFlow.dbf'.format(datapath)]
    elevslope_files = ['{}/badriver/elevslope.dbf'.format(datapath)]
    flowlines = ['{}/badriver/NHDflowlines.shp'.format(datapath)]

    lns = sfrmaker.lines.from_NHDPlus_v2(NHDFlowlines=flowlines,
                                PlusFlowlineVAA=pfvaa_files,
                                PlusFlow=plusflow_files,
                                elevslope=elevslope_files,
                                filter='{}/badriver/grid.shp'.format(datapath))
    return lns


@pytest.fixture(scope='module')
def active_area_shapefile(datapath):
    return '{}/badriver/active_area.shp'.format(datapath)


@pytest.fixture(scope='module')
def dem(datapath):
    return os.path.join(datapath, 'badriver/dem_26715.tif')


@pytest.fixture(scope='module')
def sfrmaker_grid_from_sr(tylerforks_model_grid, active_area_shapefile):
    grid = sfrmaker.StructuredGrid.from_sr(sr=tylerforks_model_grid,
                                          active_area=active_area_shapefile)
    return grid


@pytest.fixture(scope='module')
def sfrmaker_grid_from_shapefile(grid_shapefile):
    #grid = sfrmaker.StructuredGrid.from_sr(model_grid,
    #                                       active_area=active_area_shapefile)
    #return grid
    pass


@pytest.fixture
def sfrdata(tylerforks_model, lines_from_NHDPlus,
            sfrmaker_grid_from_sr):
    m = tylerforks_model
    # from the lines and StructuredGrid instances, make a sfrmaker.sfrdata instance
    # (lines are intersected with the model grid and converted to reaches, etc.)
    sfrdata = lines_from_NHDPlus.to_sfr(grid=sfrmaker_grid_from_sr,
                                    model=m)
    return sfrdata


@pytest.mark.parametrize('method', ['cell polygons', 'buffers'])
def test_sample_elevations(dem, sfrdata, datapath, method):
    sfr = sfrdata
    sampled_elevs = sfr.sample_reach_elevations(dem, method=method, smooth=True)
    sfr.reach_data['strtop'] = [sampled_elevs[rno] for rno in sfr.reach_data['rno']]
    assert reach_elevations_decrease_downstream(sfr.reach_data)


#@pytest.mark.parametrize('grid', [sfrmaker_grid_from_sr,
#                                   sfrmaker_grid_from_shapefile
#                                   ])
def test_make_sfr(outdir, sfrmaker_grid_from_sr,
                  tylerforks_model,
                  lines_from_NHDPlus,
                  active_area_shapefile,
                  dem):

    m = tylerforks_model
    sfr = lines_from_NHDPlus.to_sfr(grid=sfrmaker_grid_from_sr,
                                    model=m)
    sfr.set_streambed_top_elevations_from_dem(dem, dem_z_units='meters')

    botm = m.dis.botm.array.copy()
    layers, new_botm = sfrmaker.utils.assign_layers(sfr.reach_data, botm_array=botm)
    sfr.reach_data['k'] = layers
    if new_botm is not None:
        botm[-1] = new_botm
        np.savetxt('{}/external/botm{}.dat'.format(m.model_ws,
                                                   m.nlay - 1),
                   new_botm, fmt='%.2f')
        sfr.ModflowSfr2.parent.dis.botm = botm

    sfr.create_ModflowSfr2(model=m)
    sfr.ModflowSfr2.check()
    sfr.write_package(istcb2=223)  # writes a sfr file to the model workspace
    m.write_name_file()  # write new version of name file with sfr package

    # wite shapefiles for visualization
    sfr.export_cells(outdir + 'example_cells.shp')
    sfr.export_outlets(outdir + 'example_outlets.shp')
    sfr.export_lines(outdir + 'example_lines.shp')
    sfr.export_routing(outdir + 'example_routing.shp')

    # run the modflow model
    if shutil.which('mfnwt') is not None:
        m.exe_name = 'mfnwt'
        try:
            success, buff = m.run_model(silent=False)
        except:
            pass
        assert success, 'model run did not terminate successfully'