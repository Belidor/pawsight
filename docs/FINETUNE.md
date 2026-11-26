# Fine-tuning for your own dog

The base YOLO model is trained on COCO and knows the "dog" class in general.
Your dog is a specific breed, a specific coat colour, specific light in a
specific room. Fine-tuning raises detection confidence exactly where the base
model hesitates: a dog in someone's arms, in a cone, against the light, curled
up so that the silhouette does not read.

This step is not mandatory. The system works on the base model too. Fine-tuning
makes sense when you see in `storage/detect.log` that the dog gets lost.

---

## First, the rake everyone steps on

**Fine-tuning on a single "dog" class does not degrade the other 79 COCO
classes. It erases them.**

The model's head is rebuilt for `nc=1`, and the `bowl` class ceases to exist.
And all the zones of this project depend on `bowl`: `bowls`, `water` and `food`
are tied to the bowls found in the frame.

It breaks **silently**. After such fine-tuning the dog is detected even better
than before. Bowl visits simply vanish from the statistics for good, and you
conclude that the dog has stopped drinking.

That is why the dataset always has three classes:

```python
DATASET_CLASSES = {0: "dog", 1: "bowl", 2: "person"}
```

`person` is there so that the model does not count a human as a dog: on all
fours and in motion they get confused more often than you would think.

The flip side: **each** of the three classes must be labelled in **every**
frame. A missing bowl is not "no data", it is a training example saying "there
is no bowl here". Hence the minimal set: three classes, no more.

---

## Step 1. Accumulate frames

Collection runs by itself while the detector works (`PAWSIGHT_COLLECT_DATASET=1`
in `.env`).

```bash
.venv/bin/python -m pawsight dataset
```

Sample output:

```
=== Fine-tuning dataset ===
  frames:            127
  label files:       127
  oldest:            2026-08-30
  classes:           dog, bowl, person
  folder:            dataset/
```

The minimum for training is 50 frames, but realistically it is worth waiting
for 200-400 and several days: you need different light, different poses,
different times of day.

Not everything is saved, and that matters. At 5 fps a dog at the bowl gives 300
nearly identical frames a minute; training on them overfits the model to one
pose. So frames are selected:

- **by uncertainty**: frames where `conf` is in the middle band (0.15-0.80) are
  taken. Where the model is already confident there is nothing to teach;
- **with deduplication** by perceptual hash: near-identical frames are dropped;
- **with a cap of 40 frames per hour**, so that one busy day does not flood the
  dataset.

Negative examples accumulate separately in `dataset/negatives/`: frames where
the model saw a dog that is not there. One of those is worth ten correct ones.

---

## Step 2. Review the labels

The pre-labelling is done by the model itself, and it contains mistakes,
otherwise fine-tuning would be pointless. The easiest way to go through them is
the **"Dataset"** tab on the dashboard: it puts suspicious frames first (empty
labels, "dog only", no bowls, low confidence) and lets you edit boxes, remove
them all at once and throw out bad frames entirely. For a long review session
the separate page `http://127.0.0.1:8099/label` does the same with the frame at
full width, keyboard shortcuts and undo. Any YOLO-format editor
works too (label-studio, CVAT, labelImg).

Deleting a frame from the dashboard also touches `val_manifest.json`: the
removed frame's name is struck from the split, otherwise its share (see step 3)
would be computed over what is not on disk. Deletion does not rebuild the
split. If its share drops below 10%, it is rebuilt on the next training run
(see step 3). You can rebuild it in advance with
`pawsight dataset --refreeze-val`.

The format is standard: `dataset/labels/<name>.txt`, one line per object,
`<class> <cx> <cy> <w> <h>` as fractions from 0 to 1.

If the label files got lost or you added frames by hand:

```bash
.venv/bin/python scripts/relabel.py
```

The script restores pre-labelling **only** where there is none. It does not
touch your corrections: human edits are worth more than model predictions.

What to look at first:

- the bowls are labelled in **every** frame where they are visible, otherwise
  `bowl` will be learned as background and the zones will fall apart;
- a human is labelled as `person`, not skipped;
- the dog's box includes the tail and paws: the bottom edge gives the floor
  contact point, and that point decides which zone the dog is in.

---

## Step 3. Train

```bash
.venv/bin/python -m pawsight train
```

What happens: the dataset is split into train/val (80/20), `data.yaml` is
written, and training runs for 80 epochs with `patience=20`.

The validation split is **frozen once** and does not change afterwards: the
file `dataset/val_manifest.json`. Otherwise metrics from different weeks could
not be compared with each other. Do not delete it.

With one exception, and an unavoidable one: the dataset grows, the frozen split
does not. When its share drops below 10% of the frames, the acceptance gate
starts judging a handful of old pictures, which means letting anything through.
At that point the split is **rebuilt automatically** at the start of the run.
This is reported honestly in the log, in the run card on the dashboard and in
`train_result.json` (`val_refrozen`), because the cost is real: metrics of runs
before the rebuild are not comparable with those after it, as they were
computed on different frames.

To rebuild in advance, separately from training:

```bash
.venv/bin/python -m pawsight dataset --refreeze-val
```

This used to be a refusal to start training, with advice to type that command.
The refusal is gone: the split shrinks as the dataset grows, so it punished
exactly what the whole thing is for, labelling new frames.

Training runs on MPS with `amp=False` and `workers=0`, and not "just in case":
on MPS `amp="bf16"` crashes with a `RuntimeError`, and `amp=True` is a silent
no-op (training still runs in FP32).

This is the only part of the project that cannot be containerised: MPS exists
only on native macOS.

---

## Step 4. The acceptance gate

A new model is not accepted automatically. After training, per-class metrics
are computed (the numbers below only illustrate the format):

```
=== per-class metrics ===
  dog      mAP50=0.947  recall=0.912  precision=0.934
  bowl     mAP50=0.881  recall=0.865  precision=0.902
  person   mAP50=0.823  recall=0.790  precision=0.851
```

The model is **rejected** if:

- the `bowl` class is not among those evaluated: either there were no bowls in
  the validation set or they dropped out of the head;
- `bowl` mAP50 is below **0.70**: the zones rely on the bowls, and such a model
  must not be accepted.

Note: the threshold is on the **bowls**, not on the dog. That is deliberate.
The dog is what all of this is for, but the zones depend on the bowls, and they
are what breaks unnoticed.

Do not lower the threshold to "make it pass". If `bowl` dropped, the bowls are
poorly labelled; that is fixed with labels, not with the threshold.

### If the gate rejected it but you still want the model

This happened to the project's author, and it is an instructive case. Their
model scored `bowl` mAP50 = 0.597 against the 0.70 threshold and was rejected.
They installed it by hand, and here is what a measurement on hard frames showed
(conf threshold 0.01):

| frame | stock | fine-tuned |
|---|---|---|
| dog in a cone, lying | 0.171 | **0.727** |
| easy poses | 0.44-0.58 | 0.31-0.37 |

So the model solved the main problem (the base model barely saw the dog in a
cone) and dropped on the easy cases, but stayed above the 0.15 working
threshold.

A deliberate decision by the owner is fine. What matters is knowing what you
pay:

- the zones depend on `bowl`, and at mAP50 0.597 the bowls will be lost more
  often, and bowl visits disappear along with them;
- a 3-class model does not know furniture classes, so the camera's tilt hold
  only works while the pair of bowls is visible in the frame. Out of their view
  the tilt is not corrected.

The second point is a consequence of the head staying 3-class when `best.pt`
was exported, even though `cls_remap` was designed precisely to keep all 80
COCO classes. If you need furniture as an anchor, fine-tune on top of the full
COCO model while keeping its classes.

The detector works with any such model: the `dog` and `person` indices are
resolved by **name** from `model.names`, not by COCO numbers.

An accepted model is saved next to the base one as `yolo26s_finetuned.pt`. To
enable it:

```bash
# in .env
PAWSIGHT_MODEL=yolo26s_finetuned.pt
```

then `./stop.sh && ./start.sh`.

---

## Step 5. Make sure it got better

Validation metrics are not the same as real-time performance. Watch
`storage/detect.log` and the dashboard for a day:

- has the dog stopped getting lost where it used to?
- are bowl visits still counted? (`pawsight today`)
- are there any new false positives?

If it got worse, switch back to `PAWSIGHT_MODEL=yolo26s.pt`. The previous
weights have not gone anywhere; rolling back is free.

---

## How often

Once every week or two, while the metrics keep improving. After that, only
when something changes in reality: the bowls were moved, the lighting changed,
the dog grew up or got a haircut. Fine-tuning "just because" on the same data
is pointless.
