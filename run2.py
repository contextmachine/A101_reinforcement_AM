import numpy as np
from time import perf_counter
from contextlib import contextmanager

from A101.read_dxf import extract_polygons
from A101.poly_bbox import rect_polygons
from A101.grid_quantizer import quantize_rectilinear_loads
from A101.grid_work import clean_poly, resolve_overlaps, get_grid_matrix
from A101.calculate_mass import make_rebar_classes, loads_to_classes, rebar_summary
from A101.linear_idea import generate_all_rectangles, relabel_rectangle_candidates
from A101.select_min_density_rectangles_recipes import prepare_rectangle_problem
from A101.rectangle_solver_job import solve_rectangle_job
from A101.axis_orientation import orient_grid, grid_rectangles_to_world, normalize_axis
from A101.cells_merging import reduce_mosaic
from A101.fit_rebar_layout import fit_rebar_layout


@contextmanager
def timer(name=""):
    start = perf_counter()
    try:
        yield
    finally:
        print(f"{name}: {perf_counter() - start:.6f} сек")


if __name__ == "__main__":
    # Пользовательские параметры
    back_grid = (18, 300)
    stock = [(18, 300), (20, 150), (20, 100), (25, 150), (25, 100)]
    max_lay = 2
    min_w = 1000
    iron_dens = 7850
    anchor_k = 32
    axis = normalize_axis("y")
    N, MAX_N = 30, 100
    dxf_path = r"C:\Users\AM\Downloads\dxf\С2_t_700_Верхняя по оси У.dxf"

    start_polygons = extract_polygons(dxf_path)
    ortho_polygons = rect_polygons(start_polygons)
    main_grid = quantize_rectilinear_loads(
        ortho_polygons,
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
    quantized = clean_poly(resolve_overlaps(main_grid["snapped"]))
    xs, ys, load_matrix = get_grid_matrix(quantized)

    loads = sorted({p["load"] for p in start_polygons})
    cfg = make_rebar_classes(loads, back_grid, stock, max_lay=max_lay)
    load2cls, recipes = cfg["load2cls"], cfg["recipes"]
    densities, diameters, steps = cfg["densities"], cfg["diameters"], cfg["steps"]

    base_holds = {cls: anchor_k * diameter for cls, diameter in diameters.items()}
    holds = dict(base_holds)
    for cls, layers in recipes.items():
        holds[cls] = max(base_holds[layer] for layer in layers)

    int_matrix = loads_to_classes(load_matrix, load2cls)

    # Grid algorithms are normalized to working axis='y'. For source axis='x'
    # matrix/coordinates are swapped here and restored only after the solve.
    work_matrix, work_x_edges, work_y_edges, work_x_steps, work_y_steps = orient_grid(
        int_matrix, xs, ys, axis
    )
    work_axis = "y"

    requirement_rectangles = generate_all_rectangles(
        int_matrix=work_matrix,
        x_steps=work_x_steps,
        y_steps=work_y_steps,
        xs=work_x_edges,
        min_w=min_w,
        holds=holds,
    )
    selectable_rectangles = relabel_rectangle_candidates(requirement_rectangles, recipes)

    # Build the exact mosaic, but do not heuristically delete candidates:
    # geometrically identical class-1/class-3 variants are not interchangeable.
    work_rectangles, mosaic, _ = reduce_mosaic(
        work_matrix,
        selectable_rectangles,
        target=np.inf,
        rect_target=np.inf,
        force_reduce=False,
        show=False,
    )

    prepared = prepare_rectangle_problem(
        value_matrix=work_matrix,
        xs=work_x_steps,
        ys=work_y_steps,
        rectangles=work_rectangles,
        densities=densities,
        recipes=recipes,
        holds=base_holds,
        axis=work_axis,
        mosaic=mosaic,
        max_n=MAX_N,
        build_pulp_template=True,
    )

    print(f"Запуск основного солвера для N={N}")
    with timer("T_main"):
        result, data = solve_rectangle_job(
            prepared=prepared,
            data={},
            N=N,
            timeout=100,
            backend="highs",
            threads=16,
        )

    if not result or not result.get("is_feasible") or not result.get("rectangles"):
        raise RuntimeError(f"Основной solver не вернул допустимое решение: {result}")

    # Solver rectangles are in normalized work-grid indices. Convert with the
    # actual edge arrays (preserves non-zero origin), then restore source X/Y.
    rec_opt = grid_rectangles_to_world(
        result["rectangles"], work_x_edges, work_y_edges, axis
    )
    poly_mos = [(p["geometry"], load2cls[p["load"]]) for p in start_polygons]

    print("Запуск solver постобработки")
    with timer("T_post"):
        fit_result = fit_rebar_layout(
            polygons=poly_mos,
            rectangles=rec_opt,
            recipes=recipes,
            divisors=steps,
            densities=densities,
            min_width={k: min_w for k in steps},
            axis=axis,
            field=None,
            max_snap=600,
            min_bar_gap=50,
        )

    summary = rebar_summary(
        rec_opt=rec_opt,
        fit_result=fit_result,
        divisors=steps,
        diameters=diameters,
        density=iron_dens,
        axis=axis,
    )
    print(summary)
