# Rendering a run of the companion dataset

This note gives everything needed to render the three cameras of a run of the companion audio dataset in this Unreal Engine 5.8 project, so that the
images match the audio, the geometry and the labels of the dataset frame by frame.

## 1. Inputs, per run (from the dataset)

| file | used for |
|---|---|
| `geometry.csv` | one row every 0.05 s (20 Hz): `time_s`, `ego_E_m`, `ego_N_m`, `ego_heading_deg`, `ego_speed_kmh`, `mic_E_m`, `mic_N_m`, then for every vehicle `<name>_E_m`, `<name>_N_m`, `<name>_heading_deg`, `<name>_speed_kmh`. The `*_lat`, `*_lon` columns give the same positions in WGS84 and are not needed here. Lines starting with `#` are comments. |
| `scene.json` | `zone` (`W`, `F`, `T` or `O`) and the list `vehicules` (`nom` = name used in `geometry.csv`, `classe` = car / van / moto, `modele` = vehicle model of the new scenarios). |

When two vehicles share a name in `scene.json` (T-junction runs), the second one is `<name>2` in `geometry.csv` (`tc`, `tc2`).

## 2. Coordinates

- World frame: East-North-Up in metres, anchored at 53.174452 N, 8.168086 E. The level is built in this frame for the four zones.
- Unreal Engine (cm): **X = 100 · E**, **Y = −100 · N**, Z up.
- Headings are mathematical angles in degrees (counter-clockwise from East): **yaw_UE = −heading**.
- Ground height: a vertical line trace from z = 400 cm downwards that only accepts flat ground actors (static meshes whose bounds lie
  within ±15 cm of z = 0: ground, roads, slabs, snow patches). Walls, fences, trees and grass tufts are ignored. The ground is close to z = 0
  everywhere.

## 3. Cameras

| | |
|---|---|
| position | `(mic_E_m, mic_N_m)` of the row, **z = ground + 199 cm** |
| orientation | cam0 = left: yaw_UE = −(heading + 90); cam1 = front: yaw_UE = −heading; cam2 = right: yaw_UE = −(heading − 90); pitch 0, roll 0 |
| optics | pinhole, horizontal field of view **91.0902°**, principal point at the image centre |
| image | **1224 × 1024** pixels, one frame per row of `geometry.csv` (20 fps) |
| capture | SceneCapture2D, source *Final Color (LDR)*; after moving the actors, wait 2 editor ticks and render 3 warm-up captures before the saved one (Lumen) |

The microphone array (audio only) is centred at the same horizontal position, 1.90 m above the ground.

### Exposure (manual, per zone and per camera)

Post-process override: auto-exposure method *Manual*, *Apply Physical Camera Exposure* off, exposure compensation:

| zone | cam0 | cam1 | cam2 |
|---|---|---|---|
| W | 2.5 | 2.0 | 2.1 |
| F | 2.5 | 1.6 | 2.1 |
| T | 2.5 | 2.0 | 2.1 |
| O | 1.25 | 0.85 | 0.95 |

### Sky and light (overcast, no sun)

| zone | dome material on `SM_SkySphere` | material parameters | SkyLight intensity |
|---|---|---|---|
| W, F, T | `/Game/SiteSim/Materiaux/MI_ciel_couvert` | `Intensite` = 8.0 | 0.14 |
| O | `/Game/SiteSim/Materiaux/MI_ciel_couvert3` | `K` = 4.8, `Intensite` = 0.43 | 0.066 |

Recapture the SkyLight after changing the dome; the new lighting is used from the next frame. Global illumination: Lumen.

## 4. Vehicles

### Ego vehicle

`/Game/SiteSim/Vehicules/SM_car_ego`, scale (1.2, 1.02, 1.157), placed **0.60 m behind the camera position** along the heading,
yaw_UE = −`ego_heading_deg`, z = ground + 1 cm.

### Other vehicles

Mesh chosen from `modele`, otherwise from `nom` (trailing digits removed), otherwise from `classe`:

| key | mesh | scale (X, Y, Z) | length × width × height (m) |
|---|---|---|---|
| `bmw`, `dc` | `/Game/SiteSim/Vehicules/SM_car_BMW` | 1.195, 1.017, 1.165 | 4.71 × 1.83 × 1.44 |
| `tc`, class `car` | `/Game/SiteSim/Vehicules/SM_car_TC` | 1.08 | 4.26 × 1.94 × 1.34 |
| `t5`, class `van` | `/Game/SiteSim/Vehicules/SM_van_T5` | 1.0, 0.9, 0.95 | 4.58 × 1.90 × 2.09 |
| `idbuzz` | `/Game/SiteSim/Vehicules/SM_van_IDBuzz` | 1.0, 0.94, 0.95 | 4.58 × 1.98 × 2.09 |
| `moto`, class `moto` | `/Game/SiteSim/Vehicules/SM_moto_v2` | 1.0 | 2.17 × 0.75 × 1.17 |

All vehicle meshes have their pivot on the ground under the centre of the footprint and their front along +X. Position =
(`<name>_E_m`, `<name>_N_m`) = centre of the vehicle; yaw_UE = −`<name>_heading_deg`; z = ground + 1 cm.

## 5. Scene states

- Always hidden in the renders: actors whose label starts with `ego_W_` (a parked-car placeholder at W).
- Barrier at W: in the replays of recorded runs (`FEV`, `TJ`) the barrier is **open**: actors `W_barriere_levee_*` visible,
  `W_barriere_fermee_*` hidden. In the new scenarios (`NEW`) it is **closed**: the opposite; vehicles stop in front of it.

## 6. Outputs expected in the dataset (per run)

| file | content |
|---|---|
| `cameras/cam0.mp4`, `cam1.mp4`, `cam2.mp4` | every frame, 20 fps, H.264 (crf 18), 1224 × 1024 |
| `frames_1fps.tar` | one JPEG per second and per camera: frame 20·k + 10 (the middle of second k), named `<run>_cam<c>_<frame:05d>.jpg` |
| `frames_index.csv` | one row per frame: `image`, `time_s`, `zone`, ego position and heading, camera position in UE (cm), file of each camera, positions of the vehicles |
| `overview_3cams.mp4` | the three cameras side by side with the stereo sound and the labels |

Frame `i` corresponds to row `i` of `geometry.csv`, taken at `time_s`. The audio (`audio_8ch.flac`, 48 000 samples per second)
starts at 0 s, so frame `i` matches audio sample `round(time_s × 48000)`. The first row is at 0 s in the new scenarios (`NEW`) and the
T-junction runs (`TJ`), at 0.05 s in 280 February runs (`FEV`) and at 1.15 s in the 109 others: when muxing frames and sound, skip the first `time_s` seconds of the audio (ffmpeg: `-ss <first time_s>` before
the audio input). The stereo mix of `overview_3cams.mp4` uses microphone channels 6 (left) and 2 (right) of `audio_8ch.flac`.
