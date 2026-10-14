def in_bbox(lat, lon, bbox):
    if lat is None or lon is None:
        return False
    return (bbox['south'] <= lat <= bbox['north'] and
            bbox['west'] <= lon <= bbox['east'])

def bbox_to_esri(bbox):
    return f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"

def bbox_to_opensky(bbox):
    return {'lamin': bbox['south'], 'lomin': bbox['west'],
            'lamax': bbox['north'], 'lomax': bbox['east']}

def centroid(bbox):
    return ((bbox['north'] + bbox['south']) / 2,
            (bbox['east'] + bbox['west']) / 2)
