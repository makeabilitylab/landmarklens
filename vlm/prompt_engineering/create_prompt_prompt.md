We are making a VR tool that will place users in a virtual space constructed out of 360 panoramas we took walking along city streets. Our overall goal is to make a tool that highlights important landmarks and street features for people with poor sense of direction, to give them a better sense of the environment.

We collected a fair amount of gaze data from people with good sense of direction, and have a list of objects and features that we think would be good to highlight. The list is below, in order of priority.

To achieve this highlighting, we will pass a sequence of panoramic images taken on the streetscape to a VLM, likely Gemini 3 Pro, and ask it to identify and locate objects of interest from that list.

However, there are a few wrinkles:
1. We don't want or need the same object to be highlighted across all the images, perhaps in just one or two frames. But, since the panoramas were taken walking down the street, its highly likely that the same objects will appear across many frames, just at different angles. As such, in the sequence, a given object should only be identified a maximum of twice. The first time it appears should be the one chosen, if it appears in multiple images.
2. We don't want the user to be overloaded with highlights. So, each frame should have a *maximum* of two objects highlighted. 

Can you help me write a prompt for Gemini to achieve this task? Here is my desired inputs and outputs:

Input:
* A sequence of 5 panoramic images, named and passed in order (so, 1 comes before 2, comes before 3 and so on). They are oriented so the walking direction is in the *center of the image*. Detections should be biased towards the walking direction, though particularly eye catching features or landscapes can and should be highlighted even if they are not in the middle (e.g., Tokyo Tower)

Desired output:
* Bounding boxes of chosen objects
* Descriptions of the objects
* An "landmark/eyecatchingness" score that represents how likely the VLM believes this would assist in navigation, combining both contextual knowledge and the ranked listing
* A JSON with the results and bounding box of detected markers, structured like:

{
    'image1': [
        {'bbox': [...], 'description': 'description here', 'landmark_score': float},
        {'bbox': [...], 'description': 'description here', 'landmark_score': float},
    ],
    'image2': [
        {'bbox': [...], 'description': 'description here', 'landmark_score': float},
        {'bbox': [...], 'description': 'description here', 'landmark_score': float},
    ]
    ...
}

### Object types and rankings

Include:
* Obvious landmarks, like Tokyo Skytree, sculptures, or brightly colored signage
* Signage to do with *wayfinding*, e.g., a roadmap
* Parks and greenspaces
* Transit stations, e.g., subway entrances or bus stops
* Street furniture, e.g., benches
* Vending machines
* Visually distinctive non-residential buildings 
* Other permanent urban infrastructure, e.g., pedestrian bridges, highways, tunnels

Exclude:
* Transient objects, e.g., cars, pedestrians
* Generic signage that does not convey location information, e.g., security cameras, stop signs, speed limits, restaurant billboards
* Temporary signage that will be removed later on, e.g., store advertisements, political campaign posters, banners for upcoming events