## Common types

**FEPolygon** (represents the geometry of a single finite element in the uploaded load mosaic (scene))
```
{
    "load": 5.7,
    "color": 181,
    "points": [
      [
        100,
        0
      ],
      [
        200,
        340
      ],
      [
        100,
        406.25
      ],
      [
        100,
        406.25
      ]
    ]
  }
```

**Overlay** On the frontend, we have something like an eraser that allows certain elements to be "erased". This is needed to remove zones that should not be reinforced within the current slab slice (for example, cores in foundation mosaics are reinforced at an angle and do not directly participate in the horizontal foundation reinforcement).
There are two types: `clean` applies a mask to the elements in `idxs`; `unclean` removes the mask from the elements in `idxs`. A mask cannot be removed or applied twice. If an attempt is made to apply a mask to an already masked element, or remove a mask from an unmasked element, that element is simply skipped (so that moving the "eraser" over the same element several times does not create multiple masks that would also have to be erased several times to restore the element).
If `real`: false, reinforcement is prohibited on such polygons; essentially, polygons with this setting should be interpreted as openings, and bars must be clipped around them. If `real`: true, the element receives zero load, but bars may still be placed over it.
```
   {
      "type": "clean", // "clean"|"uncleab"
      "idxs": [3, 56, 78],
      "real": true,
      "time": 12345654,
    },
```
**RcVariant** is essentially a reinforcement variant that includes diameter and spacing. It is used extensively.
```
{"d": 18.0,     "step": 300.0}
```
**MassMetrics**: mass metrics for a specific reinforcement solution. Values are separated for background and additional reinforcement (ignore the fact that the numbers in the example are identical; they will not be identical in reality—this is only an example).
```
 {
    "additional": {
      "with_anchorage_kg": 4653.717431416138,
      "without_anchorage_kg": 2901.925976986611,
      "with_anchorage_unclipped_kg": 5120.86344379941,
      "without_anchorage_unclipped_kg": 3074.019121710686
    },
    "bg":{
         "with_anchorage_kg": 4653.717431416138,
      "without_anchorage_kg": 2901.925976986611,
      "with_anchorage_unclipped_kg": 5120.86344379941,
      "without_anchorage_unclipped_kg": 3074.019121710686
    }
  },
```



**ZoneBase**:
```
    {
      "id":0,
      "kind": "bg", 
      "arm": {"d": 18.0,     "step": 300.0},
      "anchorage":{"start":800.0,"end":800.0}  // optional; when the structure is used as input data, this can be replaced by the global "anchor_factor".
    }
      
```
**ZoneAdditional**:
```
{
"id": 1,
      "arm": {"d": 20.0,     "step": 150.0},
     "kind":"additional",
      "left": 10, // number of bars to place on the left. Total number of bars is left+right+1
      "right": 5, // number of bars to place on the right
      "length": 1730.0, // length without anchorage
      "ancorage":{"start":800.0,"end":800.0}, // optional; when the structure is used as input data, this can be replaced by the global "anchor_factor".
      "origin": [
        4860.0,
        699.999999
      ],
      "direction": [
        0.0,
        -1.0
      ]
    }
  
```

**Zone**:
```
ZoneBase | ZoneAdditional
```

**Bar**:
```
 {
        "zone_id": 0,
        "start": [
          100.0,
          50.001000000000204
        ],
        "end": [
          14200.0,
          50.001000000000204
        ],
        "d": 18.0,
        "anchorage": {"start":800.0, "end":800.0},
      }
      
```

## POST /v2/dxf_upload
Input:
```
{
  "file": string($binary)
}
```
Output:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "state": "ready" // "error"
}
```

## POST /v2/json_upload
```
// FEPolygon[]
[
  {
    "load": 5.7,
    "color": 181,
    "points": [
      [
        100,
        0
      ],
      [
        200,
        340
      ],
      [
        100,
        406.25
      ],
      [
        100,
        406.25
      ]
    ],
  }
]
```
Output:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "state": "ready" // "error"
}
```

## POST /v2/tables_upload
Input:
```
{
  "nodes_file": string($binary),
  "elements_file": string($binary),
  "loads_file": string($binary),
  "load_column": 4 // 1|2|3|4
}
```
Output:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "state": "ready" // "error"
}
```

## GET /v2/scenes/{scene_id}/polygons
Input:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "smooth": false,
  "overlay_id": 0
}
```
Output:
```
// FEPolygon[]
[
  {
    "load": 5.7,
    "color": 181,
    "points": [
      [
        100,
        0
      ],
      [
        200,
        340
      ],
      [
        100,
        406.25
      ],
      [
        100,
        406.25
      ]
    ],
    "overlay_state": "active", // "real" | "empty"
    "source_index": 0
  }
]
```

## POST /v2/scenes/{scene_id}/overlays
Input:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "overlays": Overlay[]
  /* example:
  [
    {
      "type": "clean",
      "idxs": [3, 56, 78],
      "real": true,
      "time": 12345654,
    },
    {
      "type": "unclean",
      "idxs": [4, 7],
      "real": false,
      "time": 12345667,
    }
  ]*/
}
```
Output:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "overlay_id": 67692
}
```

## GET /v2/scenes/{scene_id}/overalys/{overlay_id}
overlay_id: 12345678 - actual ID, 0 - initial, -1 - latest, -2 - second latest, etc.

Output:
```
// Overlay[]
[
  {
    "type": "clean",
    "idxs": [3, 56, 78],
    "id": 67689,
    "real": true
  },
  {
    "type": "unclean",
    "idxs": [4, 7],
    "id": 67690,
    "real": false
  }
]

```
## PUT /v2/tasks
Input:
```
{
  "scene_id": "c8f8c9048259477294bb583b5523f059",
  "overlay_id": 0, // ID of the overlay to use
  "smooth": false, // display in the interface, default false (ignore isolated single elements)
  "n": [10,20,133],
  "config": {
    "max_layers": 2,
    "axis": "x",
    "anchor_factor": 40,// anchorage length on each side for a zone or bar is defined as $anchor_factor * d$
    "min_width_mm": 300,
    "max_snap_mm": 600,
    "min_bar_gap_mm": 50,
    "steel_density_kg_m3": 7850,
    "back_grid":  RcVariant //  example: {"d": 18.0,     "step": 300.0},
    "stock": RcVariant[] /* example: [
      {"d": 18.0,     "step": 300.0},
      {"d": 20.0,     "step": 150.0},
      {"d": 20.0,     "step": 100.0},
      {"d": 25.0,     "step": 150.0},
      {"d": 25.0,     "step": 100.0}
    ],*/
    "solver": {
      "solver_time_limit": null // time that HiGHS may spend computing a solution for one N //
    }
  }
}
```
Output:
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1"
}
```

## PUT /v2/tasks/{task_id}/n
Input:
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1",
  "n": [1,2,3,4]
}
```

## PUT /v2/tasks/{task_id}/cancel
Input:
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1",
  "n": [1,2,3,4]
}
```

## GET /v2/tasks/{task_id}
Input:
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1"
}
```
Output:
```
{
   
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1",
  "scene_id": "a2b8259ba85a4c15a673a4ed4c5369b1",
  
  "smooth": false,
  "overlay_id": 0,
  "solutions": [
    {
      "n": 133,
      "state": "success", // "pending","preparing","solving","fitting","bars","error","success" ,
      
      // optional fields:
      "fun": 4894455442.621317,
      "status": "optimal"  ,
      // optimal: an optimal solution was found
      // feasable: some feasible solution was found
      // infeasable: no solution was found
      "mass_metrics": MassMetrics 
    }
  ]
  
}
```

## GET /v2/tasks/{task_id}/{n}
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1",
  "n": 50
}
```
Output:
```
{
  "task_id": "7f0b456e1eb54957aaae1f9ad4240de1",
  "n": 133,
  "state": "success", //"pending", "preparing","solving","fitting","bars","error""success"
  // optional:
  "fun": 4894455442.621317,
  "status": "optimal" ,
      // optimal: an optimal solution was found
      // feasable: some feasible solution was found
      // infeasable: no solution was found
  "mass_metrics": MassMetrics,
  "bars": Bar[],
  "zones": Zone[]

}
```



available `kind` values: `bg`, `additional`. 



## POST /v2/bars
Input:
```
{
  "scene_id": "a2b8259ba85a4c15a673a4ed4c5369b1",
  "smooth": false,
  "overlay_id": 0,
  "config": {
    "axis": "x",
    "anchor_factor": 40,
    "min_bar_gap_mm": 50
  },
  "zones": Zone[] 


}
```

Output:

```json
{
  "task_id": "09dfe739d16a4137ba7ee2dfe5586c2b",
  "state": "pending"
}
```

`state`:

* `pending` — the request has been created and is waiting for a worker;
* `running` — bar layout is in progress;
* `success` — the result is ready;
* `error` — the operation ended with an error.

Ouput:

## GET /v2/bars/{task_id}

Output on success:

```json
{
  "task_id": "09dfe739d16a4137ba7ee2dfe5586c2b",
   "state": "success"
  "bars": Bar[], //optional, if success
  "zones": Zone[], //optional, if success
  "mass_metrics": MassMetrics, //optional, if success

}
```
## POST /v2/verification
Input:
```
{
  "scene_id": "a2b8259ba85a4c15a673a4ed4c5369b1",
  "smooth": false,
  "overlay_id": 0,
  "config": {
    "axis": "x",
    "anchor_factor": 40,
    "steel_density_kg_m3": 7850,
    "t": 600,
    "min_bar_gap_mm": 50,
  },
  "zones": Zone[]
}
```
Output:

```json
{
  "task_id": "ac93aa09949345d4a4585df47e8fcf90",
  "state": "pending"
}
```

`state`:

* `pending` — the request is waiting for a worker;
* `running` — processing is in progress;
* `success` — the result is ready;
* `error` — the operation ended with an error.

## GET /v2/verification/{verification_task_id}
Output:
```json
{
  "verification_id": "ac93aa09949345d4a4585df47e8fcf90",
  "state": "success",
  
  // optional, if success:
  "result": [ 
    {
      "source_index": 0,
      "overlay_state": "active",
      "need_load_sm2/m": 5.7,
      "fact_load_sm2/m": 6.8,
      "need_load_kg/m3": 19.6,
      "fact_load_kg/m3": 28.4
    },
    ...
  ]
}
```

