from A101.read_dxf import extract_polygons
from A101.poly_bbox import rect_polygons
import A101.grid_quantizer as rgq
from A101.grid_work import clean_poly, resolve_overlaps, get_grid_matrix
from A101.clean_opt import visualize_rectangles
from A101.linear_idea import generate_all_rectangles, rectangles_to_xyxy
from A101.work_fast import run_many_with_timeout, timer
from A101.axis_orientation import orient_grid, restore_rectangles

import numpy as np
import matplotlib.pyplot as plt

if __name__ == '__main__':

    dxf_path = r"Верхнее армирование вдоль ОСИ У.dxf"
    direction = 'y' # 'x'
    start_polygons = extract_polygons(dxf_path)
    orto_polygons = rect_polygons(start_polygons)

    main_grid = rgq.quantize_rectilinear_loads(
        orto_polygons,
        target_cells_x=None,
        target_cells_y=None,
        method="exact",
        preserve_holes=True,

        max_shift_fraction=0.02,
        shrink_penalty=30.0,
        expand_penalty=1.0,
        load_gamma=2.5,
        min_shrink_tol_ratio=0.10,
        min_expand_tol_ratio=0.50,
        coord_eps=1e-6,
    )
    qunted_poly = main_grid['snapped']
    qunted_poly = resolve_overlaps(qunted_poly)
    qunted_poly = clean_poly(qunted_poly)

    xs, ys, matrix = get_grid_matrix(qunted_poly)
    unique, inverse = np.unique(matrix, return_inverse=True)
    int_matrix = inverse.reshape(matrix.shape)
    x_steps = np.diff(xs)
    y_steps = np.diff(ys)

    work_matrix, work_xs, work_ys, work_x_steps, work_y_steps = orient_grid(
        int_matrix,
        xs,
        ys,
        direction,
    )

    densities = {
        0: 1,
        1: 8.5,
        2: 16.5,
        3: 20.9,
    }

    final_vars = generate_all_rectangles(
            int_matrix=work_matrix,
            x_steps=work_x_steps,
            y_steps=work_y_steps,
            xs=work_xs,
            min_w=1000,
            holds={1: 800, 2: 900, 3: 1000},
        )

    N = 87
    S = 10

    kwargs = dict(
        value_matrix=work_matrix,
        xs=work_x_steps,
        ys=work_y_steps,
        rectangles=final_vars,
        densities=densities,
        solver_msg=False,
        threads=1,
        time_limit=100,
        require_optimal=True,
    )

    with timer("Main"):
        results = run_many_with_timeout(
            kwargs,
            range(1, N + 1, S),
            workers=16,
            timeout=110,
        )


    diff_vars = [
        result
        for result in results
        if result is not None and result["rectangles"] is not None
    ]

    for dv in diff_vars:
        dv["rectangles_original"] = restore_rectangles(
            dv["rectangles"],
            direction,
        )

    freq = {
        1: 1000/300,
        2: 1000/150,
        3: 1000/150,
    }

    mass = [dv['total_cost']/1e6 for dv in diff_vars]
    n_zones = [len(dv['rectangles']) for dv in diff_vars]


    plt.plot(n_zones, mass, 'o-')
    plt.xlabel('Количество зон')
    plt.ylabel('Масса')
    plt.grid(True)
    plt.show()


    rec_opt = rectangles_to_xyxy(diff_vars[round(len(diff_vars)/2-1)]['rectangles_original'], x_steps, y_steps)
    visualize_rectangles(
        int_matrix,
        x_steps,
        y_steps,
        rec_opt)
    plt.show()