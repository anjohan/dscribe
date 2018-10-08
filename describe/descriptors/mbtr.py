from __future__ import absolute_import, division, print_function
from builtins import super
import math
import numpy as np
import chronic

from ase.visualize import view

from scipy.spatial.distance import cdist
from scipy.sparse import lil_matrix, coo_matrix
from scipy.special import erf

from describe.core import System
from describe.descriptors import Descriptor

from describe.libmbtr.cmbtrwrapper import CMBTRWrapper


class MBTR(Descriptor):
    """Implementation of the Many-body tensor representation up to k=3.

    This implementation provides the following geometry functions:

        -k=1: atomic number
        -k=2: inverse distances
        -k=3: cosines of angles

    and the following weighting functions:

        -k=1: unity(=no weighting)
        -k=2: unity(=no weighting), exponential (:math:`e^-(sx)`)
        -k=3: unity(=no weighting), exponential (:math:`e^-(sx)`)

    You can use this descriptor for finite and periodic systems. When dealing
    with periodic systems, it is advisable to use a primitive cell, or if
    supercells are included to use normalization e.g. by volume or by the norm
    of the final vector.

    If flatten=False, a list of dense np.ndarrays for each k in ascending order
    is returned. These arrays are of dimension (n_elements x n_elements x
    n_grid_points), where the elements are sorted in ascending order by their
    atomic number.

    If flatten=True, a scipy.sparse.coo_matrix is returned. This sparse matrix
    is of size (1, n_features), where n_features is given by
    get_number_of_features(). This vector is ordered so that the different
    k-terms are ordered in ascending order, and within each k-term the
    distributions at each entry (i, j, h) of the tensor are ordered in an
    ascending order by (i * n_elements) + (j * n_elements) + (h * n_elements).

    This implementation does not support the use of a non-identity correlation
    matrix.
    """
    decay_factor = math.sqrt(2)*3

    def __init__(
            self,
            atomic_numbers,
            k,
            periodic,
            grid=None,
            weighting=None,
            normalize_by_volume=False,
            normalize_gaussians=True,
            flatten=True
            ):
        """
        Args:
            atomic_numbers (iterable): A list of the atomic numbers that should
                be taken into account in the descriptor. Notice that this is
                not the atomic numbers that are present for an individual
                system, but should contain all the elements that are ever going
                to be encountered when creating the descriptors for a set of
                systems. Keeping the number of handled elements as low as
                possible is preferable, especially if a dense representation
                (e.g. numpy array) is needed as an output.
            k (set or list): The interaction terms to consider from 1 to 3. The
                size of the final output and the time taken in creating this
                descriptor is exponentially dependent on this value.
            periodic (bool): Boolean for if the system is periodic or none. If
                this is set to true, you should provide the primitive system as
                input and then the number of periodic copies is determined from the
                'cutoff'-values specified in the weighting argument.
            grid (dictionary): This dictionary can be used to precisely control
                the broadening width, grid spacing and grid length for all the
                different terms. Example:
                    grid = {
                        "k1": {
                            "min": 1,
                            "max": 10
                            "sigma": 0.1
                            "n": 100
                        },
                        "k2": {
                            "min": 0,
                            "max": 1/0.70,
                            "sigma": 0.01,
                            "n": 100
                        },
                        ...
                    }

                Here 'min' is the minimum value of the axis, 'max' is the
                maximum value of the axis, 'sigma' is the standard devation of
                the gaussian broadening and 'n' is the number of points sampled
                on the grid.
            weighting (dictionary or string): A dictionary of weighting
                function settings for each term. Example:

                    weighting = {
                        "k2": {
                            "function": "unity",
                        },
                        "k3": {
                            "function": "exponential",
                            "scale": 0.5,
                            "cutoff": 1e-3,
                        }
                    }

                Weighting functions should be monotonically decreasing.
                The threshold is used to determine the minimum mount of
                periodic images to consider. The variable 'cutoff' determines
                the value of the weighting function after which the rest of the
                terms will be ignored. The K1 term is 0-dimensional, so
                weighting is not used. Here are the available functions and a
                description for them:

                    "unity": Constant weighting of 1 for all samples.
                    "exponential": Weighting of the form :math:`e^-(sx)`. The
                        parameter :math:`s` is given in the attribute 'scale'.

                The meaning of x changes for different terms as follows:

                    k=1: x = 0
                    k=2: x = Distance between A->B
                    k=3: x = Distance from A->B->C->A.

            normalize_by_volume (bool): Determines whether the output vectors are
                normalized by the cell volume. Defaults to false.
            normalize_gaussians (bool): Determines whether the gaussians are
                normalized to an area of 1. Defaults to true. If false, the
                normalization factor is dropped and the gaussians have the form.
                :math:`e^-(x-\mu)^2/2\sigma^2`
            flatten (bool): Whether the output of create() should be flattened
                to a 1D array. If False, a list of the different tensors is
                provided.

        Raises:
            ValueError if the given k value is not supported, or the weighting
            is not specified for periodic systems.
        """
        super().__init__(flatten)
        self.system = None
        self.k = k
        self.atomic_numbers = list(set(atomic_numbers))  # The given atomic numbers are first made into a set to remove duplicates, and then made into list for enabling ordering.
        self.grid = grid
        self.weighting = weighting
        self.periodic = periodic
        self.normalize_by_volume = normalize_by_volume
        self.normalize_gaussians = normalize_gaussians
        self.update()
        # initializing .create() level variables
        self.n_atoms_in_cell = None
        self._k1_geoms = None
        self._k1_weights = None
        self._k2_geoms = None
        self._k2_weights = None
        self._k3_geoms = None
        self._k3_weights = None
        self._axis_k1 = None
        self._axis_k2 = None
        self._axis_k3 = None

    def update(self):
        """Checks and updates variables in mbtr class.
        """
        # Check K value
        supported_k = set(range(1, 4))
        if isinstance(self.k, int):
            raise ValueError(
                "Please provide the k values that you wish to be generated as a"
                " list or set."
            )
        else:
            try:
                self.k = set(self.k)
            except Exception:
                raise ValueError(
                    "Could not make the given value of k into a set. Please "
                    "provide the k values as a list or a set."
                )
            if not self.k.issubset(supported_k):
                raise ValueError(
                    "The given k parameter '{}' has at least one invalid k value".format(self.k)
                )

        # Check the weighting information
        if self.weighting is not None:
            for k in self.k:
                if k > 1:
                    weight_info = self.weighting["k{}".format(k)]
                    function = weight_info.get("function")
                    valid_functions = set(("exponential", "unity"))
                    needed = ()
                    if function not in valid_functions:
                        raise ValueError(
                            "Unknown weighting function specified. Please use "
                            "one of the following: {}".format(valid_functions)
                        )
                    else:
                        if function == "exponential":
                            needed = ("cutoff", "scale")
                    for key in needed:
                        value = weight_info.get(key)
                        if value is None:
                            raise ValueError(
                                "Missing value for '{}' in the 'weighting' "
                                "specification given in the MBTR constructor."
                                .format(key)
                            )

        # Check that a weighting function is specified for each term k>1
        if self.periodic:
            for k in self.k:
                if k > 1:
                    error = ValueError(
                        "Periodic systems will need to have a weighting "
                        "function defined in the 'weighting' dictionary of "
                        "the MBTR constructor when requesting k > 1"
                    )
                    if self.weighting is None:
                        raise error
                    weight_info = self.weighting["k{}".format(k)]
                    if weight_info is None:
                        raise error
                    function = weight_info["function"]
                    if function is None or function == "unity":
                        raise error

        # Check the given grid
        if self.grid is not None:
            self.check_grid(self.grid)

        self.n_elements = None  # Number of elements for MBTR
        self.atomic_number_to_index = {}  # a
        self.atomic_number_to_d1 = {}
        self.atomic_number_to_d2 = {}
        self.index_to_atomic_number = {}
        self.n_copies_per_axis = None

        # Sort the atomic numbers. This is not needed but makes things maybe a
        # bit easier to debug.
        self.atomic_numbers.sort()
        for i_atom, atomic_number in enumerate(self.atomic_numbers):
            self.atomic_number_to_index[atomic_number] = i_atom
            self.index_to_atomic_number[i_atom] = atomic_number
        self.n_elements = len(self.atomic_numbers)

        self.max_atomic_number = max(self.atomic_numbers)
        self.min_atomic_number = min(self.atomic_numbers)

    def check_grid(self, grid):
        """Used to ensure that the given grid settings are valid.
        """
        if grid is not None:
            for i in self.k:
                info = grid.get("k{}".format(i))
                if info is not None:
                    msg = "The grid information is missing the value for {}"
                    val_names = ["min", "max", "sigma", "n"]
                    for val_name in val_names:
                        try:
                            info[val_name]
                        except Exception:
                            raise KeyError(msg.format(val_name))

                    # Make the n into integer
                    n = grid.get("k{}".format(i))["n"]
                    grid.get("k{}".format(i))["n"] = int(n)
                    assert info["min"] < info["max"], \
                        "The min value should be smaller than the max values"

    @chronic.time
    def describe(self, system):
        """Return the many-body tensor representation as a 1D array for the
        given system.

        Args:
            system (System): The system for which the descriptor is created.

        Returns:
            1D ndarray: The many-body tensor representation up to the k:th term
            as a flattened array.
        """
        # Initializes the scalar numbers that depend no the system
        self.initialize_scalars(system)

        return self.create_with_grid()

    @chronic.time
    def create_with_grid(self, grid=None):
        """Used to recalculate MBTR for an already seen system but with
        different grid setttings. This function can be used after
        """
        if grid is None:
            grid = self.grid
        else:
            self.check_grid(grid)
            self.grid = grid

        mbtr = []
        if 1 in self.k:

            # We will use the original system to calculate the counts, unlike
            # with the other terms that use the extended system
            settings_k1 = self.get_k1_settings()
            k1 = self.K1(settings_k1)
            mbtr.append(k1)

        if 2 in self.k:
            settings_k2 = self.get_k2_settings()
            k2 = self.K2(settings_k2)
            mbtr.append(k2)

        if 3 in self.k:

            settings_k3 = self.get_k3_settings()
            k3 = self.K3(settings_k3)

            mbtr.append(k3)

        if self.flatten:
            length = 0

            datas = []
            rows = []
            cols = []
            for tensor in mbtr:
                size = tensor.shape[1]
                coo = tensor.tocoo()
                datas.append(coo.data)
                rows.append(coo.row)
                cols.append(coo.col + length)
                length += size

            datas = np.concatenate(datas)
            rows = np.concatenate(rows)
            cols = np.concatenate(cols)
            final_vector = coo_matrix((datas, (rows, cols)), shape=[1, length], dtype=np.float32)

            if self.normalize_by_volume:
                volume = self.system.get_volume()
                final_vector /= volume
            return final_vector

        # Normalize with respect to cell volume if requested
        else:
            if self.normalize_by_volume:
                volume = self.system.get_volume()
                for part in mbtr:
                    part /= volume
            return mbtr

    def get_original_system_limit(self, system):
        """Used to return the limit of atoms considered to be part of the
        original system.
        """
        return len(system)

    def initialize_scalars(self, system):
        """Used to initialize the scalar values for each k-term.
        """
        # Ensuring variables are re-initialized when a new system is introduced
        self.n_atoms_in_cell = None
        self.system = system
        self._k1_geoms = None
        self._k1_weights = None
        self._k2_geoms = None
        self._k2_weights = None
        self._k3_geoms = None
        self._k3_weights = None
        self._axis_k1 = None
        self._axis_k2 = None
        self._axis_k3 = None

        self.update()

        self.n_atoms_in_cell = self.get_original_system_limit(system)
        present_element_numbers = set(system.numbers)
        self.present_indices = set()
        for number in present_element_numbers:
            try:
                index = self.atomic_number_to_index[number]
            except KeyError:
                raise KeyError(
                    "The given systems contains atomic element {} that has not "
                    "been declared in 'atomic_numbers' given in the class "
                    "constructor.".format(number)
                )
            self.present_indices.add(index)

        if 1 in self.k:
            self.k1_geoms_and_weights(system)
        if 2 in self.k:
            # If needed, create the extended system
            system_k2 = system
            if self.periodic:
                system_k2 = self.create_extended_system(system, 2)
            self.k2_geoms_and_weights(system_k2)

            # Free memory
            system_k2 = None

        if 3 in self.k:
            # If needed, create the extended system
            system_k3 = system
            if self.periodic:
                system_k3 = self.create_extended_system(system, 3)
            self.k3_geoms_and_weights(system_k3)

            # Free memory
            system_k3 = None

    def get_k1_settings(self):
        """Returns the min, max, dx and sigma for K1.
        """
        if self.grid is not None and self.grid.get("k1") is not None:
            return self.grid["k1"]
        else:
            sigma = 1e-1
            min_k = self.min_atomic_number-MBTR.decay_factor*sigma
            max_k = self.max_atomic_number+MBTR.decay_factor*sigma
            return {
                "min": min_k,
                "max": max_k,
                "sigma": sigma,
                "n": int(math.ceil((max_k-min_k)/sigma/4) + 1),
            }

    def get_k2_settings(self):
        """Returns the min, max, dx and sigma for K2.
        """
        if self.grid is not None and self.grid.get("k2") is not None:
            return self.grid["k2"]
        else:
            sigma = 2**(-7)
            min_k = 0-MBTR.decay_factor*sigma
            max_k = 1/0.7+MBTR.decay_factor*sigma
            return {
                "min": min_k,
                "max": max_k,
                "sigma": sigma,
                "n": int(math.ceil((max_k-min_k)/sigma/4) + 1),
            }

    def get_k3_settings(self):
        """Returns the min, max, dx and sigma for K3.
        """
        if self.grid is not None and self.grid.get("k3") is not None:
            return self.grid["k3"]
        else:
            sigma = 2**(-3.5)
            min_k = -1.0-MBTR.decay_factor*sigma
            max_k = 1.0+MBTR.decay_factor*sigma
            return {
                "min": min_k,
                "max": max_k,
                "sigma": sigma,
                "n": int(math.ceil((max_k-min_k)/sigma/4) + 1),
            }

    def get_number_of_features(self):
        """Used to inquire the final number of features that this descriptor
        will have.

        Returns:
            int: Number of features for this descriptor.
        """
        n_features = 0
        n_elem = self.n_elements

        if 0 in self.k:
            n_k0_grid = self.get_k0_settings()["d1"]["n"]
            n_k0 = 2*n_k0_grid
            n_features += n_k0
        if 1 in self.k:
            n_k1_grid = self.get_k1_settings()["n"]
            n_k1 = n_elem*n_k1_grid
            n_features += n_k1
        if 2 in self.k:
            n_k2_grid = self.get_k2_settings()["n"]
            n_k2 = (n_elem*(n_elem+1)/2)*n_k2_grid
            n_features += n_k2
        if 3 in self.k:
            n_k3_grid = self.get_k3_settings()["n"]
            n_k3 = (n_elem*n_elem*(n_elem+1)/2)*n_k3_grid
            n_features += n_k3

        return int(n_features)

    def create_extended_system(self, primitive_system, term_number):
        """Used to create a periodically extended system, that is as small as
        possible by rejecting atoms for which the given weighting will be below
        the given threshold.

        Args:
            primitive_system (System): The original primitive system to
                duplicate.
            term_number (int): The term number of the tensor. For k=2, the max
                distance is x, for k>2, the distance is given by 2*x.

        Returns:
            System: The new system that is extended so that each atom can at
            most have a weight that is larger or equivalent to the given
            threshold.
        """
        numbers = primitive_system.numbers
        relative_pos = primitive_system.get_scaled_positions()
        cartesian_pos = np.array(primitive_system.get_positions())
        cell = primitive_system.get_cell()

        # Determine the upper limit of how many copies we need in each cell
        # vector direction. We take as many copies as needed for the
        # exponential weight to come down to the given threshold.
        cell_vector_lengths = np.linalg.norm(cell, axis=1)
        n_copies_axis = np.zeros(3, dtype=int)
        weight_info = self.weighting["k{}".format(term_number)]
        weighting_function = weight_info["function"]
        cutoff = self.weighting["k{}".format(term_number)]["cutoff"]

        if weighting_function == "exponential":
            scale = weight_info["scale"]
            function = lambda x: np.exp(-scale*x)

        for i_axis, axis_length in enumerate(cell_vector_lengths):
            limit_found = False
            n_copies = -1
            while (not limit_found):
                n_copies += 1
                distance = n_copies*cell_vector_lengths[0]

                # For terms k>2 we double the distances to take into
                # account the "loop" that is required.
                if term_number > 2:
                    distance = 2*distance

                weight = function(distance)
                if weight < cutoff:
                    n_copies_axis[i_axis] = n_copies
                    limit_found = True

        # Create copies of the cell but keep track of the atoms in the
        # original cell
        num_extended = []
        pos_extended = []
        num_extended.append(numbers)
        pos_extended.append(cartesian_pos)
        a = np.array([1, 0, 0])
        b = np.array([0, 1, 0])
        c = np.array([0, 0, 1])
        for i in range(-n_copies_axis[0], n_copies_axis[0]+1):
            for j in range(-n_copies_axis[1], n_copies_axis[1]+1):
                for k in range(-n_copies_axis[2], n_copies_axis[2]+1):
                    if i == 0 and j == 0 and k == 0:
                        continue
                    num_copy = np.array(numbers)

                    # Calculate the positions of the copied atoms and filter
                    # out the atoms that are farther away than the given
                    # cutoff.
                    pos_copy = np.array(relative_pos)-i*a-j*b-k*c
                    pos_copy_cartesian = np.dot(pos_copy, cell)

                    # In MBTR we consider all distances from the original cell
                    # to atoms in periodic copies of the system.
                    positions_to_consider = cartesian_pos
                    distances = cdist(pos_copy_cartesian, positions_to_consider)

                    # For terms above k==2 we double the distances to take into
                    # account the "loop" that is required.
                    if term_number > 2:
                        distances *= 2

                    weights = function(distances)
                    weight_mask = weights >= cutoff

                    # Create a boolean mask that says if the atom is within the
                    # range from at least one atom in the original cell
                    valids_mask = np.any(weight_mask, axis=1)

                    valid_pos = pos_copy_cartesian[valids_mask]
                    valid_num = num_copy[valids_mask]

                    pos_extended.append(valid_pos)
                    num_extended.append(valid_num)

        pos_extended = np.concatenate(pos_extended)
        num_extended = np.concatenate(num_extended)

        extended_system = System(
            positions=pos_extended,
            numbers=num_extended,
            cell=cell,
            pbc=False
        )

        return extended_system

    def gaussian_sum(self, centers, weights, settings):
        """Calculates a discrete version of a sum of Gaussian distributions.

        The calculation is done through the cumulative distribution function
        that is better at keeping the integral of the probability function
        constant with coarser grids.

        The values are normalized by dividing with the maximum value of a
        gaussian with the given standard deviation.

        Args:
            centers (1D np.ndarray): The means of the gaussians.
            weights (1D np.ndarray): The weights for the gaussians.
            settings (dict): The grid settings

        Returns:
            Value of the gaussian sums on the given grid.
        """
        start = settings["min"]
        stop = settings["max"]
        sigma = settings["sigma"]
        n = settings["n"]

        dx = (stop - start)/(n-1)
        x = np.linspace(start-dx/2, stop+dx/2, n+1)
        pos = x[np.newaxis, :] - centers[:, np.newaxis]
        y = weights[:, np.newaxis]*1/2*(1 + erf(pos/(sigma*np.sqrt(2))))
        f = np.sum(y, axis=0)

        if not self.normalize_gaussians:
            max_val = 1/(sigma*math.sqrt(2*math.pi))
            f /= max_val

        f_rolled = np.roll(f, -1)
        pdf = (f_rolled - f)[0:-1]/dx  # PDF is the derivative of CDF

        return pdf

    @chronic.time
    def k1_geoms_and_weights(self, system):
        """Calculate the atom count for each element.

        Args:
            system (System): The atomic system.

        Returns:
            1D ndarray: The counts for each element in a list where the index
            of atomic number x is self.atomic_number_to_index[x]
        """
        if self._k1_geoms is None or self._k1_weights is None:

            cmbtr = CMBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                self.n_atoms_in_cell
            )

            # For k=1, the geometry function is given by the atomic number, and
            # the weighting function is unity by default.
            parameters = {}
            self._k1_geoms, self._k1_weights = cmbtr.get_k1_geoms_and_weights(geom_func=b"atomic_number", weight_func=b"unity", parameters=parameters)
        return self._k1_geoms, self._k1_weights

    @chronic.time
    def k2_geoms_and_weights(self, system):
        """Calculates the value of the geometry function and corresponding
        weights for unique two-body combinations.

        Args:
            system (System): The atomic system.

        Returns:
            dict: Inverse distances in the form: {(i, j): [list of angles] }.
            The dictionaries are filled so that the entry for pair i and j is
            in the entry where j>=i.
        """
        if self._k2_geoms is None or self._k2_weights is None:

            cmbtr = CMBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                self.n_atoms_in_cell
            )

            # Determine the weighting function
            if self.weighting is not None:
                weight_info = self.weighting["k2"]
                weighting_function = weight_info["function"]
            else:
                weighting_function = "unity"
            parameters = {}
            if weighting_function == "exponential":
                parameters = {
                    b"scale": weight_info["scale"],
                    b"cutoff": weight_info["cutoff"]
                }

            self._k2_geoms, self._k2_weights = cmbtr.get_k2_geoms_and_weights(
                geom_func=b"inverse_distance",
                weight_func=weighting_function.encode(),
                parameters=parameters
            )
        return self._k2_geoms, self._k2_weights

    @chronic.time
    def k3_geoms_and_weights(self, system):
        """Calculates the value of the geometry function and corresponding
        weights for unique three-body combinations.

        Args:
            system (System): The atomic system.

        Returns:
            tuple: (geoms, weights) Cosines of the angles (values between -1
            and 1) in the form {(i,j,k): [list of angles] }. The weights
            corresponding to the angles are stored in a similar dictionary.
        """
        if self._k3_geoms is None or self._k2_weights is None:

            # Calculate the angles with the C++ implementation
            cmbtr = CMBTRWrapper(
                system.get_positions(),
                system.get_atomic_numbers(),
                self.atomic_number_to_index,
                self.n_atoms_in_cell
            )

            # Determine the weighting function
            if self.weighting is not None:
                weight_info = self.weighting["k3"]
                weighting_function = weight_info["function"]
            else:
                weighting_function = "unity"
            parameters = {}
            if weighting_function == "exponential":
                parameters = {
                    b"scale": weight_info["scale"],
                    b"cutoff": weight_info["cutoff"]
                }

            self._k3_geoms, self._k3_weights = cmbtr.get_k3_geoms_and_weights(geom_func=b"cosine", weight_func=weighting_function.encode(), parameters=parameters)

        return self._k3_geoms, self._k3_weights

    def K1(self, settings):
        """Calculates the first order terms where the scalar mapping is the
        number of atoms of a certain type.

        Args:
            system (System): Atomic system.
            settings (dict): Grid settings.

        Returns:
            ndarray | scipy.sparse.lil_matrix: K1 values.
        """
        start = settings["min"]
        stop = settings["max"]
        n = settings["n"]
        self._axis_k1 = np.linspace(start, stop, n)

        n_elem = self.n_elements
        k1_geoms, k1_weights = self._k1_geoms, self._k1_weights

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k1 = lil_matrix((1, n_elem*n), dtype=np.float32)
        else:
            k1 = np.zeros((n_elem, n), dtype=np.float32)

        for m, key in enumerate(sorted(k1_geoms.keys())):
            i = key[0]

            geoms = np.array(k1_geoms[key])
            weights = np.array(k1_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, settings)

            if self.flatten:
                start = i*n
                end = (i+1)*n
                k1[0, start:end] = gaussian_sum
            else:
                k1[i, :] = gaussian_sum

        return k1

    def K2(self, settings):
        """Calculates the second order terms where the scalar mapping is the
        inverse distance between atoms.

        Args:
            settings (dict): The grid settings

        Returns:
            1D ndarray: flattened K2 values.
        """
        start = settings["min"]
        stop = settings["max"]
        n = settings["n"]
        self._axis_k2 = np.linspace(start, stop, n)

        k2_geoms, k2_weights = self._k2_geoms, self._k2_weights
        n_elem = self.n_elements

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k2 = lil_matrix(
                (1, int(n_elem*(n_elem+1)/2*n)), dtype=np.float32)
        else:
            k2 = np.zeros((self.n_elements, self.n_elements, n))

        for m, key in enumerate(sorted(k2_geoms.keys())):
            i = key[0]
            j = key[1]

            geoms = np.array(k2_geoms[key])
            weights = np.array(k2_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, settings)

            if self.flatten:
                start = m*n
                end = (m + 1)*n
                k2[0, start:end] = gaussian_sum
            else:
                k2[i, j, :] = gaussian_sum

        return k2

    def K3(self, settings):
        """Calculates the third order terms where the scalar mapping is the
        angle between 3 atoms.

        Args:
            settings (dict): The grid settings

        Returns:
            1D ndarray: flattened K3 values.
        """
        start = settings["min"]
        stop = settings["max"]
        n = settings["n"]
        self._axis_k3 = np.linspace(start, stop, n)

        k3_geoms, k3_weights = self._k3_geoms, self._k3_weights
        n_elem = self.n_elements

        # Depending of flattening, use either a sparse matrix or a dense one.
        if self.flatten:
            k3 = lil_matrix(
                (1, int(n_elem*n_elem*(n_elem+1)/2*n)), dtype=np.float32)
        else:
            k3 = np.zeros((n_elem, n_elem, n_elem, n))

        for m, key in enumerate(sorted(k3_geoms.keys())):
            i = key[0]
            j = key[1]
            k = key[2]

            geoms = np.array(k3_geoms[key])
            weights = np.array(k3_weights[key])

            # Broaden with a gaussian
            gaussian_sum = self.gaussian_sum(geoms, weights, settings)

            if self.flatten:
                start = m*n
                end = (m+1)*n
                k3[0, start:end] = gaussian_sum
            else:
                k3[i, j, k, :] = gaussian_sum

        return k3
