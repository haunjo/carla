# autopilot_data_capture.py

"""
Modified version of the scenario-based data capture script to instead run a live
CARLA simulation with an autopilot agent (e.g., BehaviorAgent) and record
sensor data (e.g., RGB camera, instance segmentation, etc.) directly.
"""
import os
import sys
import glob
import gc
import time
import math
import json
import argparse
import numpy as np
import cv2
from queue import Queue
import carla
import numpy.random as random

try:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/carla')
except IndexError:
    pass

# ========== Constants ==========

SENSORS = [
    [
        'ring_front_center',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 1550, 'image_size_y': 2048, 'fov': 50,
            'x': 0.53, 'y': 0.00, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
        }
    ],
    [
        'ring_front_left',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.45, 'y': -0.2, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': -45.0
        }
    ],
    [
        'ring_front_right',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.45, 'y': 0.2, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': 45.0
        }
    ],
    [
        'ring_rear_left',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.0, 'y': -0.12, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': -153.0
        }
    ],
    [
        'ring_rear_right',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.0, 'y': 0.12, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': 153.0
        }
    ],
    [
        'ring_side_left',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.21, 'y': -0.27, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': -99.2
        }
    ],
    [
        'ring_side_right',
        {
            'bp': 'sensor.camera.rgb',
            'image_size_x': 2048, 'image_size_y': 1550, 'fov': 60,
            'x': 0.21, 'y': 0.27, 'z': 1.70, 'roll': 0.0, 'pitch': 0.0, 'yaw': 99.2
        }
    ]
]

state2int = {
    carla.TrafficLightState.Red: 0,
    carla.TrafficLightState.Yellow: 1,
    carla.TrafficLightState.Green: 2,
    carla.TrafficLightState.Off: 3,
    carla.TrafficLightState.Unknown: 4
}


FPS = 1
SAMPLE_NUM = 20
SAVE_PATH = 'train'


def get_next_folder_path(root_dir):
    os.makedirs(root_dir, exist_ok=True)
    existing = [d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d)) and d.isdigit()]
    next_number = int(sorted(existing)[-1]) + 1 if existing else 1
    next_path = os.path.join(root_dir, f"{next_number:03d}")
    os.makedirs(next_path, exist_ok=False)
    return next_path


def are_waypoints_connected(prev_wp, curr_wp):
    """
    두 waypoint가 연결된 도로상에 있거나 인접한 경우 True 반환
    """
    next_wps = prev_wp.next(10.0)  
    for next_wp in next_wps:
        if next_wp.road_id == curr_wp.road_id:
            return True

    return False

def build_projection_matrix(w, h, fov):
    focal = w / (2.0 * np.tan(fov * np.pi / 360.0))
    K = np.identity(3)
    K[0, 0] = K[1, 1] = focal
    K[0, 2] = w / 2.0
    K[1, 2] = h / 2.0
    return K

def is_within_fov(cam_forward, cam_location, target_location, fov_deg):
    ray = target_location - cam_location
    ray_norm = ray.make_unit_vector()
    dot = cam_forward.dot(ray_norm)
    if dot <= 0:
        return False
    angle = math.degrees(math.acos(dot))
    return angle < (fov_deg / 2.0)


def modify_vehicle_physics(actor):
    try:
        physics_control = actor.get_physics_control()
        physics_control.use_sweep_wheel_collision = True
        actor.apply_physics_control(physics_control)
    except Exception:
        pass

def save_data_to_disk(sensor_id, frame, data, imu_data, endpoint):
    """
    Saves the sensor data into file:
    - Images                        ->              '.png', one per frame, named as the frame id
    - Lidar:                        ->              '.ply', one per frame, named as the frame id
    - SemanticLidar:                ->              '.ply', one per frame, named as the frame id
    - RADAR:                        ->              '.csv', one per frame, named as the frame id
    - GNSS:                         ->              '.csv', one line per frame, named 'gnss_data.csv'
    - IMU:                          ->              '.csv', one line per frame, named 'imu_data.csv'
    """

    if isinstance(data, carla.Image):
        sensor_endpoint = f"{endpoint}/{sensor_id}/{frame}.jpg"
        os.makedirs(os.path.dirname(sensor_endpoint), exist_ok=True)
        img = np.reshape(np.copy(data.raw_data), (data.height, data.width, 4))
        cv2.imwrite(sensor_endpoint, img)
        # data.save_to_disk(sensor_endpoint, color_converter=carla.ColorConverter.Raw)

    elif isinstance(data, carla.LidarMeasurement):
        sensor_endpoint = f"{endpoint}/{sensor_id}/{frame}.ply"
        data.save_to_disk(sensor_endpoint)

    elif isinstance(data, carla.SemanticLidarMeasurement):
        sensor_endpoint = f"{endpoint}/{sensor_id}/{frame}.ply"
        data.save_to_disk(sensor_endpoint)

    elif isinstance(data, carla.RadarMeasurement):
        sensor_endpoint = f"{endpoint}/{sensor_id}/{frame}.csv"
        os.makedirs(os.path.dirname(sensor_endpoint), exist_ok=True)
        data_txt = f"Altitude,Azimuth,Depth,Velocity\n"
        for point_data in data:
            data_txt += f"{point_data.altitude},{point_data.azimuth},{point_data.depth},{point_data.velocity}\n"
        with open(sensor_endpoint, 'w') as data_file:
            data_file.write(data_txt)

    elif isinstance(data, carla.GnssMeasurement):
        sensor_endpoint = f"{endpoint}/{sensor_id}/gnss_data.csv"
        os.makedirs(os.path.dirname(sensor_endpoint), exist_ok=True)
        with open(sensor_endpoint, 'a') as data_file:   
            data_txt = f"{frame},{data.altitude},{data.latitude},{data.longitude}\n"
            data_file.write(data_txt)

    elif isinstance(data, carla.IMUMeasurement):
        sensor_endpoint = f"{endpoint}/{sensor_id}/imu_data.csv"
        os.makedirs(os.path.dirname(sensor_endpoint), exist_ok=True)
        with open(sensor_endpoint, 'a') as data_file:
            data_txt = f"{frame},{imu_data[0][0]},{imu_data[0][1]},{imu_data[0][2]},{data.compass},{imu_data[1][0]},{imu_data[1][1]},{imu_data[1][2]}\n"
            data_file.write(data_txt)

    else:
        print(f"WARNING: Ignoring sensor '{sensor_id}', as no callback method is known for data of type '{type(data)}'.")

def carla_to_openlane_pose(x, y, z, roll, pitch, yaw):
    Rx = np.array([
        [1, 0, 0],
        [0, np.cos(roll), -np.sin(roll)],
        [0, np.sin(roll),  np.cos(roll)]
    ])
    Ry = np.array([
        [np.cos(pitch), 0, np.sin(pitch)],
        [0, 1, 0],
        [-np.sin(pitch), 0, np.cos(pitch)]
    ])
    Rz = np.array([
        [np.cos(yaw), -np.sin(yaw), 0],
        [np.sin(yaw),  np.cos(yaw), 0],
        [0, 0, 1]
    ])
    R_carla = Rz @ Ry @ Rx
    flip_Y = np.diag([1, -1, 1])
    R_openlane = flip_Y @ R_carla
    U, _, Vt = np.linalg.svd(R_openlane)
    R_openlane_fixed = U @ Vt
    if np.linalg.det(R_openlane_fixed) < 0:
        Vt[-1, :] *= -1
        R_openlane_fixed = U @ Vt
    t_openlane = flip_Y @ np.array([x, y, z])
    return R_openlane_fixed.tolist(), t_openlane.tolist()

def save_annotations(vehicle, frame, save_path, traffics):
    pose_path = "openlane_v2_default.json"
    os.makedirs(save_path, exist_ok=True)
    transform = vehicle.get_transform()
    location = transform.location
    rotation = transform.rotation
    R, t = carla_to_openlane_pose(round(location.x, 6), round(location.y, 6), round(location.z, 6),
                                  round(rotation.roll, 6), round(rotation.pitch, 6), round(rotation.yaw, 6))

    anno = {}
    if os.path.exists(pose_path):
        with open(pose_path, 'r') as f:
            anno = json.load(f)
    else:
        anno['pose'] = {}
        anno['sensor'] = {}
        anno['annotation'] = {'traffic_element': []}

    anno['pose']['rotation'] = R
    anno['pose']['translation'] = t
    anno['pose']['heading'] = rotation.yaw
    anno['timestamp'] = frame
    anno['segment_id'] = os.path.basename(save_path)
    for i in anno.get('sensor', {}).keys():
        anno['sensor'][i]['image_path'] = f"{save_path}/{i}/{frame}.jpg"
    if traffics is not None:
        anno['annotation']['traffic_element'] = [
            {'id': bb['id'], 'points': bb['points'], 'category': bb['category'], 'attribute': bb['attribute']}
            for bb in traffics
        ]
    os.makedirs(os.path.join(save_path, "info"), exist_ok=True)
    with open(os.path.join(save_path, "info", f"{frame}.json"), 'w') as f:
        json.dump(anno, f, indent=2)


def sensor_callback(sensor_id, queue):
    def _callback(data):
        queue.put((sensor_id, data.frame, data))
    return _callback

def get_image_point(loc, K, w2c, debug=False, actor_id=None):
    point = np.array([loc.x, loc.y, loc.z, 1])
    point_camera = np.dot(w2c, point)
    point_camera = [point_camera[1], -point_camera[2], point_camera[0]]

    if point_camera[2] <= 0.1:
        if debug:
            print(f"[⚠️] Actor {actor_id} is behind camera. Camera z: {point_camera[2]:.2f}")
        return None

    img_point = np.dot(K, point_camera)
    img_point /= img_point[2]
    x, y = img_point[0], img_point[1]

    if debug and (x < 0 or y < 0 or x > K[0, 2] * 2 or y > K[1, 2] * 2):
        print(f"[❌] Actor {actor_id} projected outside image bounds: ({x:.1f}, {y:.1f})")
    return np.array([x, y])

def get_rotation_matrix(yaw, pitch, roll):
    yaw_rad = math.radians(yaw)
    pitch_rad = math.radians(pitch)
    roll_rad = math.radians(roll)
    R_yaw = np.array([
        [math.cos(yaw_rad), -math.sin(yaw_rad), 0],
        [math.sin(yaw_rad),  math.cos(yaw_rad), 0],
        [0, 0, 1]
    ])
    R_pitch = np.array([
        [math.cos(pitch_rad), 0, math.sin(pitch_rad)],
        [0, 1, 0],
        [-math.sin(pitch_rad), 0, math.cos(pitch_rad)]
    ])
    R_roll = np.array([
        [1, 0, 0],
        [0, math.cos(roll_rad), -math.sin(roll_rad)],
        [0, math.sin(roll_rad),  math.cos(roll_rad)]
    ])
    return R_yaw @ R_pitch @ R_roll

def get_bbox_facing(bbox_rot):
    R = get_rotation_matrix(bbox_rot.yaw, bbox_rot.pitch, bbox_rot.roll)
    y_axis = np.array([0, 1, 0])
    return R @ y_axis

def bbox_sanity_check(x_min, x_max, y_min, y_max, image_w, image_h, min_wh=10, max_area_ratio=0.5):
    w = x_max - x_min
    h = y_max - y_min
    area = w * h
    img_area = image_w * image_h
    if w < min_wh or h < min_wh:
        return False
    if area > max_area_ratio * img_area:
        return False
    if x_max <= x_min or y_max <= y_min:
        return False
    return True

def get_projected_bbox(verts, K, world_2_camera, image_w, image_h):
    x_max = -1e6
    x_min = 1e6
    y_max = -1e6
    y_min = 1e6
    valid_projection = True
    for vert in verts:
        p = get_image_point(vert, K, world_2_camera)
        if p is None or np.isnan(p[0]) or np.isnan(p[1]):
            valid_projection = False
            break
        x, y = p
        x_min = min(x_min, x)
        x_max = max(x_max, x)
        y_min = min(y_min, y)
        y_max = max(y_max, y)
    if not valid_projection:
        return None, None, None, None, False
    x_min = int(max(0, min(image_w - 1, x_min)))
    x_max = int(max(0, min(image_w - 1, x_max)))
    y_min = int(max(0, min(image_h - 1, y_min)))
    y_max = int(max(0, min(image_h - 1, y_max)))
    return x_min, x_max, y_min, y_max, True

def filter_traffic_lights(vehicle, K, world_2_camera, image_w, image_h):
    traffics = []
    id = 0
    # 모든 traffic light actor 탐색 (bounding_box_set이 아닌 traffic_light actor 기준)
    tls = [a for a in vehicle.get_world().get_actors() if 'traffic_light' in a.type_id]
    for tl in tls:
        id = tl.get_opendrive_id()
        state = tl.state
        category_val = state2int.get(state, 4)  # default 4: Unknown

        tl_loc = tl.get_location()
        ve_loc = vehicle.get_transform().location
        dist = math.hypot(tl_loc.x - ve_loc.x, tl_loc.y - ve_loc.y)
        if not (3 < dist < 50):
            continue

        forward_vec = vehicle.get_transform().get_forward_vector()
        ray = tl.get_transform().location - ve_loc
        if forward_vec.dot(ray) <= 0:
            continue
        
        for idx, bbox in enumerate(tl.get_light_boxes()):
            bbox_center = bbox.location
            if bbox_center.z < 2:
                continue
            bbox_facing = get_bbox_facing(bbox.rotation)
            vehicle_forward = vehicle.get_transform().get_forward_vector()
            v_forward = np.array([vehicle_forward.x, vehicle_forward.y, vehicle_forward.z])
            dot = np.dot(bbox_facing, v_forward)
            # vehicle 기준 불빛이 정면을 향하는 경우만 (마주봄 75도 이내)
            if dot > -math.cos(math.radians(75)):
                continue

            verts = bbox.get_local_vertices()
            x_min, x_max, y_min, y_max, valid_projection = get_projected_bbox(
                verts, K, world_2_camera, image_w, image_h
            )
            if not valid_projection:
                continue
            if not bbox_sanity_check(x_min, x_max, y_min, y_max, image_w, image_h):
                continue
            
            wp = tl.get_affected_lane_waypoints()[idx]  
            traffics.append({
                'id': f"tl_{str(id)}_{idx}",
                'points': [[x_min, y_min], [x_max, y_max]],
                'category': category_val,
                'attribute': [wp.transform.location.x, wp.transform.location.y, wp.transform.location.z]

            })
    return traffics

def main():
    endpoint = get_next_folder_path(SAVE_PATH)
    os.makedirs(endpoint, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=2000)
    parser.add_argument('--duration', type=int, default=120, help='Duration to run (sec)')
    args = parser.parse_args()

    client = carla.Client(args.host, args.port)
    client.set_timeout(10.0)
    world = client.get_world()

    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = 1.0 / FPS
    traffic_manager = client.get_trafficmanager()
    traffic_manager.set_synchronous_mode(True)
    world.apply_settings(settings)

    blueprint_library = world.get_blueprint_library()
    vehicle_bp = blueprint_library.filter('vehicle.lincoln.mkz')[0]
    vehicle_bp.set_attribute('role_name', 'hero')
    
    vehicle = None
    spawn_points = world.get_map().get_spawn_points()
    if not spawn_points:
        print('No spawn points available in map.')
        sys.exit(1)
        
    while vehicle is None:
        spawn_point = random.choice(spawn_points)
        vehicle = world.try_spawn_actor(vehicle_bp, spawn_point)
        if vehicle:
            modify_vehicle_physics(vehicle)



    start_waypoint = world.get_map().get_waypoint(vehicle.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving)
    waypoints = [start_waypoint]
    curr_wp = start_waypoint
    for _ in range(SAMPLE_NUM - 1):
        next_wps = curr_wp.next(5.0)
        if not next_wps:
            break
        curr_wp = random.choice(next_wps)
        waypoints.append(curr_wp)
        
    sensor_queue = Queue()
    sensors = []
    for sensor_id, config in SENSORS:
        bp = blueprint_library.find(config['bp'])
        for attr, value in config.items():
            if attr in ['bp', 'x', 'y', 'z', 'roll', 'pitch', 'yaw']:
                continue
            bp.set_attribute(attr, str(value))

        transform = carla.Transform(
            carla.Location(x=config['x'], y=config['y'], z=config['z']),
            carla.Rotation(pitch=config['pitch'], roll=config['roll'], yaw=config['yaw'])
        )
        sensor = world.spawn_actor(bp, transform, attach_to=vehicle)
        sensor.listen(sensor_callback(sensor_id, sensor_queue))
        sensors.append(sensor)

    front_cam = sensors[0]
    image_w, image_h, fov = int(front_cam.attributes['image_size_x']), int(front_cam.attributes['image_size_y']), float(front_cam.attributes['fov'])
    K = build_projection_matrix(image_w, image_h, fov)

    try:
        for i, wp in enumerate(waypoints):
            
            vehicle.set_transform(wp.transform)

            world.tick()
            time.sleep(1.00)
                    
            while not sensor_queue.empty():
                try:
                    sensor_queue.get_nowait()
                except Exception:
                    break
                                                        
            world.tick()

            for _ in range(len(SENSORS)):
                try:
                    sensor_id, frame_id, data = sensor_queue.get(timeout=2.0)
                    save_data_to_disk(sensor_id, frame_id, data, [[0,0,0],[0,0,0]], endpoint)
                except Exception as e:
                    print(f"[ERROR] Sensor data receive failed: {e}")
            
            world_2_camera = np.array(front_cam.get_transform().get_inverse_matrix())
            traffics = []
            traffics = filter_traffic_lights(vehicle, K, world_2_camera, image_w, image_h)

            save_annotations(vehicle, frame_id, endpoint, traffics)
            print(f"[{i+1}/{len(waypoints)}] Frame saved at {vehicle.get_transform()}")
            gc.collect()
    finally:
        for s in sensors:
            s.stop()
            s.destroy()
        vehicle.destroy()
        settings.synchronous_mode = False
        settings.fixed_delta_seconds = None
        world.apply_settings(settings)

if __name__ == '__main__':
    main()
