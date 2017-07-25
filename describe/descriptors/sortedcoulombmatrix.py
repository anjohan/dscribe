import numpy as np
from describe.descriptors import Descriptor


class SortedCoulombMatrix(Descriptor):
    """Calculates the sorted and zero padded Coulomb matrix for different
    systems.

    The Coulomb matrix is defined as:

        C_ij = 0.5 Zi**exponent      | i = j
             = (Zi*Zj)/(Ri-Rj)	     | i != j

    The rows of this matrix are further sorted by their euclidean norm, such
    that the matrix C satisfies ||C_i|| >= ||C_(i+1)|| \forall i, where C_i
    denotes the ith row of the Coulomb matrix.

    The matrix is also padded with invisible atoms, which means that the matrix
    is padded with zeros until the maximum allowed size defined by n_max_atoms
    is reached.
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
            ndarray: The zero padded Coulomb matrix either as a 2D array or as
                a 1D array depending on the setting self.flatten.
        """
        cmat = self.coulomb_matrix(system)
        if self.flatten:
            cmat = cmat.flatten()
        return cmat

    def coulomb_matrix(self, system):
        """Creates the Coulomb matrix for the given system.
        """
        # Calculate offdiagonals
        q = system.charges
        qiqj = q[None, :]*q[:, None]
        idmat = system.inverse_distance_matrix
        cmat = qiqj*idmat

        # Set diagonal
        np.fill_diagonal(cmat, 0.5 * q ** 2.4)

        # Sort the atoms such that the norms of the rows are in descending
        # order
        norms = np.linalg.norm(cmat, axis=1)
        sorted_indices = np.argsort(norms, axis=0)[::-1]
        cmat = cmat[sorted_indices]
        cmat = cmat[:, sorted_indices]

        # Pad with zeros
        zeros = np.zeros((self.n_atoms_max, self.n_atoms_max))
        zeros[:cmat.shape[0], :cmat.shape[1]] = cmat
        cmat = zeros

        return cmat

    def get_number_of_features(self):
        """Used to inquire the final number of features that this descriptor
        will have.

        Returns:
            int: Number of features for this descriptor.
        """
        return int(self.n_atoms_max**2)
