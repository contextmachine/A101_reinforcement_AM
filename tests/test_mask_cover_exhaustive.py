import itertools
import numpy as np
import pytest
from A101.max_n_milp import maximal_mask_rectangles, minimum_rectangle_cover


def all_rectangles(mask):
    ny, nx = mask.shape
    return [(x0,y0,x1,y1) for y0 in range(ny) for y1 in range(y0,ny)
            for x0 in range(nx) for x1 in range(x0,nx) if mask[y0:y1+1,x0:x1+1].all()]

@pytest.mark.parametrize('shape', [(2,3), (3,2)])
def test_enumeration_contains_every_maximal_rectangle_for_all_small_masks(shape):
    for bits in itertools.product([False, True], repeat=shape[0]*shape[1]):
        mask = np.array(bits).reshape(shape)
        all_rects = all_rectangles(mask)
        maximal = {r for r in all_rects if not any(q != r and q[0] <= r[0] and q[1] <= r[1]
                     and q[2] >= r[2] and q[3] >= r[3] for q in all_rects)}
        assert set(maximal_mask_rectangles(mask)) == maximal

def test_cover_count_equals_full_candidate_milp_for_hole_and_staircases():
    for mask in [np.array([[1,1,1],[1,0,1],[1,1,1]], bool),
                 np.tril(np.ones((4,4), bool)), np.array([[1,0,1,1],[1,1,1,0]], bool)]:
        assert minimum_rectangle_cover(mask)['count'] == minimum_rectangle_cover(mask, all_rectangles(mask))['count']
