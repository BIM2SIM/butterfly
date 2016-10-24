"""Butterfly OpenFOAM Case."""
import os
import re  # to check input names
from shutil import rmtree  # to remove case folders if needed
from distutils.dir_util import copy_tree  # to copy sHM meshes over to tri
from collections import namedtuple
from copy import deepcopy

from .version import Version
from .utilities import mkdir, wfile, runbatchfile, readLastLine, \
    loadSkippedProbes, loadCaseFiles
from .geometry import bfGeometryFromStlFile, calculateMinMaxFromBFGeometries
from .refinementRegion import refinementRegionsFromStlFile
from .meshingparameters import MeshingParameters

#
from .foamfile import FoamFile

# constant folder objects
from .turbulenceProperties import TurbulenceProperties
from .RASProperties import RASProperties
from .transportProperties import TransportProperties
from .g import G

# 0 folder objects
from .U import U
from .k import K
from .p import P
from .nut import Nut
from .epsilon import Epsilon
from .T import T
from .alphat import Alphat
from .p_rgh import P_rgh
from .conditions import ABLConditions, InitialConditions

# # system folder objects
from .blockMeshDict import BlockMeshDict
from .controlDict import ControlDict
from .snappyHexMeshDict import SnappyHexMeshDict
from .fvSchemes import FvSchemes
from .fvSolution import FvSolution
from .functions import Probes
from .decomposeParDict import DecomposeParDict

from runmanager import RunManager


class Case(object):
    """OpenFOAM Case.

    Attributes:
        name: Case name as a string with no whitespace.
        foamfiles: Collection of FoamFile objects (dictionaries) for this case.
        geometries: Collection of geometry-like objects. Geometries should have
            a name and toStlString method. The information related to boundary
            condition should already be included in related foamfiles. If you
            want to initiate the class form a folder or a number of BFGeometries
            use fromFolder, and fromBFGeometries classmethods.
    """

    SUBFOLDERS = ('0', 'constant', 'constant\\polyMesh',
                    'constant\\triSurface', 'system', 'log')

    def __init__(self, name, foamfiles, geometries):
        """Init case."""
        # original name is a variable to address the current limitation to change
        # the name of stl file in snappyHexMeshDict. It will be removed once the
        # limitation is addressed.
        self.__originalName = None

        self.projectName = name
        self.__version = float(Version.OFVer)
        self.decomposeParDict = None

        # optional input for changing working directory
        # should not be used on OpenFOAM on Windows
        self.workingDir = os.path.join(os.path.expanduser('~'), 'butterfly')
        self.__foamfiles = foamfiles

        # set foamfiles dynamically. This is flexible but makes documentation
        # tricky. also autocomplete won't work for this cases.
        for ff in foamfiles:
            if not ff:
                continue
            assert hasattr(ff, 'isFoamFile'), '{} is not a FoamFile'.format(ff)
            setattr(self, ff.name, ff)

        # set butterfly geometries
        self.__geometries = self.__checkInputGeometries(geometries)

        # place holder for refinment regions
        # use .addRefinementRegions to add regions to case
        self.__refinementRegions = []
        self.runmanager = RunManager(self.projectName)

    # TODO: Parse boundary conditions for each geometry
    @classmethod
    def fromFolder(cls, path, name=None):
        """Create a Butterfly case from a case folder.

        Args:
            path: Full path to case folder.
            name: An optional new name for this case.
        """
        # collect foam files
        __originalName = os.path.split(path)[-1]
        if not name:
            name = __originalName

        _files = loadCaseFiles(path, True)

        # convert files to butterfly objects
        ff = tuple(cls.__createFoamfileFromFile(p)
                   for f in (_files.zero, _files.constant, _files.system)
                   for p in f)

        # add stl objects

        # find snappyHexMeshDict
        sHMD = cls.__getFoamFileByName('snappyHexMeshDict', ff)

        sHMD.projectName = name

        bfGeometries = tuple(geo for f in _files.stl
                             for geo in bfGeometryFromStlFile(f)
                             if os.path.split(f)[-1][:-4]
                             in sHMD.stlFileNames)

        _case = cls(name, ff, bfGeometries)

        refinementRegions = tuple(
            ref for f in _files.stl
            if os.path.split(f)[-1][:-4] in sHMD.refinementRegionNames
            for ref in refinementRegionsFromStlFile(
                f, sHMD.refinementRegionMode(os.path.split(f)[-1][:-4]))
            )

        _case.addRefinementRegions(refinementRegions)

        # original name is a variable to address the current limitation to change
        # the name of stl file in snappyHexMeshDict. It will be removed once the
        # limitation is addressed.
        _case.__originalName = __originalName

        return _case

    @classmethod
    def fromBFGeometries(cls, name, geometries, blockMeshDict=None,
                         meshingParameters=None):
        """Create a case from Butterfly geometries.

        foamFiles/dictionaries will be generated based on boundary condition of
        geometries. fvSolution and fvSchemes will be set to default can can be
        overwritten once a Solution is created from a Case and a Recipe. You can
        overwrite them through the recipe.

        Args:
            name: Case name as a string with no whitespace.
            geometries: Collection of BFGeometries. FoamFiles/dictionaries will
                be generated based on boundary condition of geometries.
            blockMeshDict: Optional input for blockMeshDict. If blockMeshDict is
                not provided, it will be calculated from geometries in XY
                direction and boundary condition for faces will be set to
                BoundingBoxBoundaryCondition. Use BlockMeshDict to create the blockMeshDict if your case is not aligned to XY direction or you
                need to assign different boundary condition to geometries.
            meshingParameters: Optional input for MeshingParameters.
        """
        geometries = cls.__checkInputGeometries(geometries)

        # update meshingParameters
        if not meshingParameters:
            meshingParameters = MeshingParameters()

        # create foam files
        if not blockMeshDict:
            minPt, maxPt = calculateMinMaxFromBFGeometries(geometries)
            blockMeshDict = BlockMeshDict.fromMinMax(minPt, maxPt)

        blockMeshDict.updateMeshingParameters(meshingParameters)

        snappyHexMeshDict = SnappyHexMeshDict.fromBFGeometries(name, geometries)
        snappyHexMeshDict.updateMeshingParameters(meshingParameters)

        # constant folder
        if float(Version.OFVer) < 3:
            turbulenceProperties = RASProperties()
        else:
            turbulenceProperties = TurbulenceProperties()
        transportProperties = TransportProperties()
        g = G()

        # 0 floder
        _geometries = geometries + blockMeshDict.geometry
        u = U.fromBFGeometries(_geometries)
        p = P.fromBFGeometries(_geometries)
        k = K.fromBFGeometries(_geometries)
        epsilon = Epsilon.fromBFGeometries(_geometries)
        nut = Nut.fromBFGeometries(_geometries)
        t = T.fromBFGeometries(_geometries)
        alphat = Alphat.fromBFGeometries(_geometries)
        p_rgh = P_rgh.fromBFGeometries(_geometries)

        # system folder
        fvSchemes = FvSchemes()
        fvSolution = FvSolution()
        controlDict = ControlDict()
        probes = Probes()

        foamFiles = (blockMeshDict, SnappyHexMeshDict, turbulenceProperties,
                     transportProperties, g, u, p, k, epsilon, nut, t, alphat,
                     p_rgh, fvSchemes, fvSolution, controlDict, probes)

        # create case
        return cls(name, foamFiles, geometries)

    @property
    def projectName(self):
        """Project name."""
        return self.__projectName

    @projectName.setter
    def projectName(self, name):
        assert re.match("^[a-zA-Z0-9_]*$", name), \
            'Invalid project name: "{}".\n' \
            'Do not use whitespace or special charecters.'.format(name)
        self.__projectName = name

    @property
    def geometries(self):
        """Butterfly geometries."""
        return self.__geometries

    @property
    def workingDir(self):
        """Change default working directory.

        Do not change the working dir if you are using OpenFOAM for Windows
        to run the analysis.
        """
        return self.__workingDir

    @workingDir.setter
    def workingDir(self, p):
        self.__workingDir = os.path.normpath(p)

    @property
    def projectDir(self):
        """Get project directory."""
        return os.path.join(self.workingDir, self.projectName)

    @property
    def zeroFolder(self):
        """Folder 0 fullpath."""
        return os.path.join(self.projectDir, '0')

    @property
    def constantFolder(self):
        """constant folder fullpath."""
        return os.path.join(self.projectDir, 'constant')

    @property
    def systemFolder(self):
        """system folder fullpath."""
        return os.path.join(self.projectDir, 'system')

    @property
    def logFolder(self):
        """bash folder fullpath."""
        return os.path.join(self.projectDir, 'bash\\log')

    @property
    def polyMeshFolder(self):
        """polyMesh folder fullpath."""
        return os.path.join(self.projectDir, 'constant\\polyMesh')

    @property
    def triSurfaceFolder(self):
        """triSurface folder fullpath."""
        return os.path.join(self.projectDir, 'constant\\triSurface')

    @property
    def postProcessingFolder(self):
        """triSurface folder fullpath."""
        return os.path.join(self.projectDir, 'postProcessing')

    @property
    def foamFiles(self):
        """Get all the foamFiles."""
        return tuple(f for f in self.__foamfiles)

    @property
    def refinementRegions(self):
        """Get refinement regions."""
        return self.__refinementRegions

    @property
    def isPolyMeshSnappyHexMesh(self):
        """Check if the mesh in polyMesh folder is snappyHexMesh."""
        return len(os.listdir(self.polyMeshFolder)) > 5

    @property
    def probes(self):
        """Get and set Probes."""
        return self.__probes

    @probes.setter
    def probes(self, inp):
        if not inp:
            return

        assert hasattr(inp, 'probeLocations'), \
            "Expected Probes not {}".format(type(inp))

        self.__probes = inp
        if self.probes.probesCount > 0:
            # include probes in controlDict
            self.controlDict.include = self.probes.filename

    def getFoamFileByName(self, name):
        """Get a foamfile by name."""
        return self.__getFoamFileByName(name, self.foamFiles)

    def getSnappyHexMeshFolders(self):
        """Return sorted list of numerical folders."""
        _f = [int(name) for name in os.listdir(self.projectDir)
              if (name.isdigit() and
                  os.path.isdir(os.path.join(self.projectDir,
                                             name, 'polyMesh'))
                  )]

        _f.sort()

        return tuple(str(f) for f in _f)

    def getResultFolders(self):
        """Return sorted list of numerical folders."""
        _f = [int(name) for name in os.listdir(self.projectDir)
              if (name != '0' and name.isdigit() and
                  os.path.isdir(os.path.join(self.projectDir, name)) and
                  not os.path.isdir(os.path.join(self.projectDir, name, 'polyMesh'))
                  )]

        _f.sort()

        return tuple(str(f) for f in _f)

    def getFoamFilesFromLocation(self, location=None):
        """Get foamFiles in a specific location (0, constant, system)."""
        if not location:
            return tuple(f for f in self.__foamfiles)
        else:
            return tuple(f for f in self.__foamfiles
                         if f.location == '"{}"'.format(location))

    def addRefinementRegions(self, refinementRegions):
        """Add a collections of refinement regions."""
        for refinementRegion in refinementRegions:
            self.addRefinementRegion(refinementRegion)

    def addRefinementRegion(self, refinementRegion):
        """Add a refinement region."""
        assert hasattr(refinementRegion, 'isRefinementRegion'), \
            "{} is not a refinement region.".format(refinementRegion)

        self.__refinementRegions.append(refinementRegion)
        assert hasattr(self, 'snappyHexMeshDict'), \
            'You can\'t add a refinementRegion to a case with no snappyHexMeshDict.'

        self.snappyHexMeshDict.addRefinementRegion(refinementRegion)

    def copySnappyHexMesh(self, folderNumber=None, overwrite=True):
        """Copy the results of snappyHexMesh to constant/polyMesh."""
        # pick the last numerical folder
        if folderNumber:
            _s = os.path.join(self.projectDir, str(folderNumber), 'polyMesh')
            assert os.path.isdir(_s), "Can't find {}.".format(_s)
        else:
            _folders = self.getSnappyHexMeshFolders()
            if not _folders:
                return
            _s = os.path.join(self.projectDir, _folders[-1], 'polyMesh')

        # copy files to constant/polyMesh
        if overwrite:
            self.removePolyMeshContent()

        try:
            copy_tree(_s, self.polyMeshFolder)
        except Exception as e:
            print "Failed to copy snappyHexMesh folder: {}".format(e)

    def renameSnappyHexMeshFolders(self, add=True):
        """Rename snappyHexMesh numerical folders to name.org  and vice versa.

        Args:
            add: Set to True to add .org at the end of the file. Set to False
                to rename them back to the original naming.
        """
        # find list of folders in project and collect the numbers
        if not add:
            _folders = (name for name in os.listdir(self.projectDir)
                        if (name.endswith('.org') and
                        os.path.isdir(os.path.join(self.projectDir, name,
                                                   'polyMesh'))))

            for f in _folders:
                os.rename(os.path.join(self.projectDir, f),
                          os.path.join(self.projectDir, f.replace('.org', '')))
        else:
            _folders = self.getSnappyHexMeshFolders()

            # rename them starting from 1
            for f in _folders:
                try:
                    os.rename(os.path.join(self.projectDir, f),
                              os.path.join(self.projectDir, '%s.org' % f))
                except Exception as e:
                    raise Exception('Failed to rename snappyHexMesh folders: {}'.format(e))

    def removeSnappyHexMeshFolders(self):
        """Remove snappyHexMesh numerical folders.

        Use this to clean the folder.
        """
        self.renameSnappyHexMeshFolders(add=False)
        _folders = self.getSnappyHexMeshFolders()

        for f in _folders:
            try:
                rmtree(os.path.join(self.projectDir, f))
            except Exception as e:
                print 'Failed to remove {}:\n{}'.format(_f, e)

    def removeResultFolders(self):
        """Remove results folder."""
        _folders = self.getResultFolders()
        for _f in _folders:
            try:
                rmtree(os.path.join(self.projectDir, _f))
            except Exception as e:
                print 'Failed to remove {}:\n{}'.format(_f, e)

    def removePostProcessingFolder(self):
        """Remove post postProcessing folder."""
        if not os.path.isdir(self.postProcessingFolder):
            return

        try:
            rmtree(self.postProcessingFolder)
        except Exception as e:
            print 'Failed to remove postProcessing folder:\n{}'.format(e)

    def removePolyMeshContent(self):
        """Remove files inside polyMesh folder."""
        for _f in os.listdir(self.polyMeshFolder):
            if _f != 'blockMeshDict':
                _fp = os.path.join(self.polyMeshFolder, _f)
                if os.path.isfile(_fp):
                    os.remove(_fp)
                elif os.path.isdir(_fp):
                    rmtree(_fp)

    def purge(self, removePolyMeshContent=True,
              removeSnappyHexMeshFolders=True,
              removeResultFolders=False,
              removePostProcessingFolder=False):
        """Purge case folder."""
        if removePolyMeshContent:
            self.removePolyMeshContent()
        if removeSnappyHexMeshFolders:
            self.removeSnappyHexMeshFolders()
        if removeResultFolders:
            self.removeResultFolders()
        if removePostProcessingFolder:
            self.removePostProcessingFolder()

    def updateBCInZeroFolder(self):
        """Update boundary conditions in files in 0 folder.

        Call this method if you have made any changes to boundary condition of
        any of the geometries after initiating the class.
        """
        raise NotImplementedError()

    def save(self, overwrite=False):
        """Save case to folder.

        Args:
            overwrite: If True all the current content will be overwritten
                (default: False).
        """
        # create folder and subfolders if they are not already created
        if overwrite and os.path.exists(self.projectDir):
            rmtree(self.projectDir, ignore_errors=True)

        for f in self.SUBFOLDERS:
            p = os.path.join(self.projectDir, f)
            if not os.path.exists(p):
                try:
                    os.makedirs(p)
                except Exception as e:
                    raise IOError(
                        'Butterfly failed to create {}\n{}'.format(p, e)
                    )

        # save foamfiles
        for f in self.foamFiles:
            f.save(self.projectDir)

        # write bfgeometries to stl file
        stlStr = (geo.toSTL() for geo in self.geometries)
        stlName = self.__originalName or self.projectName
        with open(os.path.join(self.triSurfaceFolder,
                               '%s.stl' % stlName), 'wb') as stlf:
            stlf.writelines(stlStr)

        # write refinementRegions to stl files
        for ref in self.refinementRegions:
            ref.writeToStl(self.triSurfaceFolder)

        print '{} is saved to:\n{}'.format(self.projectName, self.projectDir)

    def command(self, cmd, args=None, decomposeParDict=None, run=True, wait=True):
        u"""Run an OpenFOAM command for this case.

        This method creates a bat file under bashFolder for each command.
        The output will be logged under bash\\log as filename.log.

        Args:
            cmd: OpenFOAM command.
            args: Command arguments.
            decomposeParDict: Optional input for decomposeParDict to run analysis
                in parallel if desired.
            run: Run the command in shell.
            wait: Wait until the command is over.

        returns:
            If run is True returns a namedtuple for
                (success, error, process, logfiles, errorfiles).

                success: as a boolen.
                error: None in case of success otherwise the error message as
                    a string.
                process: Popen process.
                logfiles: List of fullpath to log files.
                errorfiles: List of fullpath to error files.
            else return a namedtuple for
                (cmd, logfiles, errorfiles)
                cmd: command lines.
                logfiles: A tuple for log files.
                errorfiles: A tuple for error files.
        """

        if not run:
            cmdlog = self.runmanager.command(cmd, args, decomposeParDict)
            return cmdlog
        else:
            log = namedtuple('log', 'success error process logfiles errorfiles')

            p, logfiles, errfiles = self.runmanager.run(cmd, args,
                                                        decomposeParDict, wait)

            logfiles = tuple(os.path.normpath(os.path.join(self.projectDir, f))
                             for f in logfiles)

            errfiles = tuple(os.path.normpath(os.path.join(self.projectDir, f))
                             for f in errfiles)

            # check error files and raise and error
            if wait:
                self.runmanager.checkFileContents(logfiles, mute=False)
                hascontent, content = self.runmanager.checkFileContents(errfiles)

                return log(not hascontent, content, p, logfiles, errfiles)
            else:
                # return namedtuple if it's all fine.
                return log(True, None, None, logfiles, errfiles)

    def blockMesh(self, args=None, wait=True, overwrite=True,):
        """Run blockMesh.

        Args:
            args: Command arguments.
            wait: Wait until command execution ends.
            overwrite: Overwrite current content of the folder.
        Returns:
            namedtuple(success, error, process, logfiles, errorfiles).
        """
        if overwrite:
            self.removePolyMeshContent()

        return self.command('blockMesh', args, decomposeParDict=None,
                            wait=wait)

    def snappyHexMesh(self, args=None, wait=True):
        """Run snappyHexMesh.

        Args:
            args: Command arguments.
            wait: Wait until command execution ends.

        Returns:
            namedtuple(success, error, process, logfiles, errorfiles).
        """
        return self.command('snappyHexMesh', args, self.decomposeParDict,
                            wait=wait)

    def checkMesh(self, args=None, wait=True):
        """Run checkMesh.

        Args:
            args: Command arguments.
            wait: Wait until command execution ends.

        Returns:
            namedtuple(success, error, process, logfiles, errorfiles).
        """
        return self.command('checkMesh', args, self.decomposeParDict,
                            wait=wait)

    def calculateMeshOrthogonality(self, useCurrntCheckMeshLog=False):
        """Calculate max and average mesh orthogonality.

        If average values is more than 80, try to generate a better mesh.
        You can use this values to set discretization schemes.
        try case.setFvSchemes(averageOrthogonality)
        """
        if not useCurrntCheckMeshLog:
            log = self.checkMesh(args=('latestTime',))
            assert log.success, log.error

        f = os.path.join(self.logFolder, 'checkMesh.log')
        assert os.path.isfile(f), 'Failed to find {}.'.format(f)

        with open(f, 'rb') as inf:
            results = ''.join(inf.readlines())
            maximum, average = results \
                .split('Mesh non-orthogonality Max:')[-1] \
                .split('average:')[:2]

            average = average.split('\n')[0]

        return float(maximum), float(average)

    @staticmethod
    def __getFoamFileByName(name, foamfiles):
        """Get a foamfile by name."""
        for f in foamfiles:
            if f.name == name:
                return f

    @staticmethod
    def __createFoamfileFromFile(p):
        """
        Create a foamfile object from an OpenFOAM foamfile.

        Args:
            p: Fullpath to file.
        """
        __foamfilescollection = {
            'turbulenceProperties': TurbulenceProperties,
            'RASProperties': RASProperties,
            'transportProperties': TransportProperties, 'g': G,
            'U': U, 'k': K, 'p': P, 'nut': Nut, 'epsilon': Epsilon, 'T': T,
            'alphat': Alphat, 'p_rgh': P_rgh, 'ABLConditions': ABLConditions,
            'initialConditions': InitialConditions,
            'blockMeshDict': BlockMeshDict, 'snappyHexMeshDict': SnappyHexMeshDict,
            'controlDict': ControlDict, 'fvSchemes': FvSchemes,
            'fvSolution': FvSolution, 'probes': Probes,
            'decomposeParDict': DecomposeParDict
        }

        name = os.path.split(p)[-1]

        if name in __foamfilescollection:
            try:
                return __foamfilescollection[name].fromFile(p)
            except Exception as e:
                print 'Failed to import {}:\n\t{}'.format(p, e)
        else:
            return FoamFile.fromFile(p)

    @staticmethod
    def __checkInputGeometries(geos):
        for geo in geos:
            assert hasattr(geo, 'isBFMesh'), \
                'Expected butterfly.Mesh not {}'.format(geo)
        return geos

    def loadMesh(self):
        """Return OpenFOAM mesh as a Rhino mesh."""
        # This is a abstract property which should be implemented in subclasses
        raise NotImplementedError()

    def loadPoints(self):
        """Return OpenFOAM mesh as a Rhino mesh."""
        # This is a abstract property which should be implemented in subclasses
        raise NotImplementedError()

    def duplicate(self):
        """Return a copy of this object."""
        return deepcopy(self)

    def ToString(self):
        """Overwrite .NET ToString method."""
        return self.__repr__()

    def __repr__(self):
        """OpenFOAM CASE."""
        return "OpenFOAM CASE: %s" % self.projectName