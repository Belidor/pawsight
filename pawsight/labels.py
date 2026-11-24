"""Dataset classes, WITHOUT cv2 and numpy, so the module imports in the container.

`dataset.py` and `train.py` pull in OpenCV at module level, and the "brain"
image does not have it and must not (it has no torch, ultralytics or cv2: the
image is measured in tens of megabytes). But the dashboard needs the same
class names and the same acceptance threshold as the detector. So the shared
part lives here: stdlib only.

WHY THERE IS MORE THAN THE DOG HERE. Fine-tuning on a single class does not
"degrade" the other 79 COCO classes, it erases them: the head is rebuilt for
nc=1, and the bowl class ceases to exist. Every zone in the project is tied to
bowl, so labelling would break silently. Details in dataset.py and train.py.
"""
from __future__ import annotations

# Indices are CONTIGUOUS (0,1,2): data.yaml requires that.
# The mapping to COCO goes by NAME: when fine-tuning, ultralytics 8.4.141
# carries over the pretrained head's rows by class name (cls_remap), so the
# order does not matter, but the set of classes does.
DATASET_CLASSES: dict[int, str] = {0: "dog", 1: "bowl", 2: "person"}
COCO_TO_DATASET: dict[int, int] = {16: 0, 45: 1, 0: 2}

# Reverse mapping: name -> dataset index. NAMES come over the wire from the
# dashboard, not indices; see the cls_remap comment above.
DATASET_IDS: dict[str, int] = {v: k for k, v in DATASET_CLASSES.items()}

# COCO index of the dog: also the sign that "the thing all this is for is in the frame".
COCO_DOG = 16


def ds_map_from_names(names: dict[int, str]) -> dict[int, int]:
    """MODEL indices -> dataset indices, by class NAME.

    The numbers depend on the model: in COCO dog=16, in the three-class
    fine-tuned model dog=0, and zero in COCO is person. Treating the mapping as
    a constant means one day writing a dog into the dataset as a person, and
    silently: the file gets created, training runs, and the error surfaces only
    in the metrics a week later.

    For exactly the same reason the detector resolves dog/person by name
    (see _ensure_classes); this is the same rule for the dataset.
    """
    return {idx: DATASET_IDS[nm] for idx, nm in names.items() if nm in DATASET_IDS}

# Minimum number of "frame+labels" pairs to start training. PAIRS are counted:
# train.py::_split drops frames without labels, and the threshold applies to
# what remains.
#
# This is the ONLY start condition, and the owner decided so: 50 frames means
# you can train without going to a console. Everything else that used to grey
# out the button either fixes itself (the validation split below) or is not a
# refusal. The quality threshold sits not here but at the output: the bowl
# acceptance gate (BOWL_MAP50_FLOOR) is still there and will most likely reject
# a model trained on fifty frames; that is a normal answer, not a failure.
MIN_IMAGES = 50

# Validation split fraction and the minimum fraction below which the
# acceptance gate stops meaning anything. Measured on the live system: the
# manifest was frozen at 8 names with 146 pairs, 5.5%, i.e. the bowl threshold
# was decided by eight old pictures.
#
# VAL_MIN_FRACTION is NOT A REFUSAL but a "time to rebuild" signal: a shrunken
# fraction fixes itself in train.py::_split. Refusing here was doubly unfair:
# the split shrinks as the dataset grows, so it punished labelling, and the
# only cure was a terminal command.
VAL_FRACTION = 0.2
VAL_MIN_FRACTION = 0.10

# Threshold below which a new model is not accepted: the bowls matter more
# than the dog, because the zones hang on them.
BOWL_MAP50_FLOOR = 0.70
