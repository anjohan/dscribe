import numpy as np
from describe.descriptors import Descriptor


class SortedSineMatrix(Descriptor):
    """Calculates the zero padded Sine matrix for different systems.

    The Sine matrix is defined as:

        Cij = 0.5 Zi**exponent      | i = j
            = (Zi*Zj)/phi(Ri, Rj)   | i != j

    where phi(r1, r2) = | B * sum(k = x,y,z)[ek * sin^2(pi * ek * B^-1
    (r2-r1))] | (B is the matrix of basis cell vectors, ek are the unit
    vectors)

    The rows of this matrix are further sorted by their euclidean norm, such
    that the matrix C satisfies ||C_i|| >= ||C_(i+1)|| \forall i, where C_i
    denotes the ith row of the Coulomb matrix.

    The matrix is also padded with invisible atoms, which means that the matrix is
    padded with zeros until the maximum allowed size defined by n_max_atoms is
    reached.
    """
    def __init__(self, n_atoms_max, flatten=True):
        """
        Args:
            n_max_atoms (int): The maximum nuber of atoms that any of the
                samples can have. This controls how much zeros need to be
                padded to the final result.
            flatten (bool): Whether the output of create() should be flattened
                to a 1D array.
        """
        super().__init__(flatten)
        self.n_atoms_max = n_atoms_max

    def describe(self, system):
        """
        Args:
            system (System): Input system.

        Returns:
            ndarray: The zero padded Sine matrix either as a 2D array or as
                a 1D array depending on the setting self.flatten.
        """
        smat = self.sine_matrix(system)
        if self.flatten:
            smat = smat.flatten()
        return smat

    def sine_matrix(self, system):
        """Creates the Sine matrix for the given system.
        """
        # Cell and inverse cell
        B = system.lattice._matrix
        B_inv = system.lattice.inv_matrix

        # Difference vectors in tensor 3D-tensor-form
        diff_tensor = system.displacement_tensor

        # Calculate phi
        arg_to_sin = np.pi * np.dot(diff_tensor, B_inv)
        phi = np.linalg.norm(np.dot(np.sin(arg_to_sin)**2, B), axis=2)

        with np.errstate(divide='ignore'):
            phi = np.reciprocal(phi)

        # Calculate Z_i*Z_j
        q = system.charges
        qiqj = q[None, :]*q[:, None]

        # Multiply by charges
        smat = qiqj*phi

        # Set diagonal
        np.fill_diagonal(smat, 0.5 * q ** 2.4)

        # Sort the atoms such that the norms of the rows are in descending
        # order
        norms = np.linalg.norm(smat, axis=1)
        sorted_indices = np.argsort(norms, axis=0)[::-1]
        smat = smat[sorted_indices]
        smat = smat[:, sorted_indices]

        # Pad with zeros
        zeros = np.zeros((self.n_atoms_max, self.n_atoms_max))
        zeros[:smat.shape[0], :smat.shape[1]] = smat
        smat = zeros

        return smat

    def get_number_of_features(self):
        """Used to inquire the final number of features that this descriptor
        will have.

        Returns:
            int: Number of features for this descriptor.
        """
        return int(self.n_atoms_max**2)
