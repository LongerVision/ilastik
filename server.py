from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Thread
from typing import Dict, List, Tuple, Optional, Hashable
import io
import json
import os
import flask
from flask import Flask, request, Response, send_file
from flask_cors import CORS
import uuid
import numpy as np
import urllib
from PIL import Image as PilImage
import argparse
from pathlib import Path

from ndstructs import Point5D, Slice5D, Shape5D, Array5D
from ndstructs.datasource import DataSource, DataSourceSlice, SequenceDataSource
from ndstructs.utils import JsonSerializable, from_json_data
from ilastik.annotations import Annotation
from ilastik.classifiers.pixel_classifier import (
    PixelClassifier,
    Predictions,
    VigraPixelClassifier,
    ScikitLearnPixelClassifier,
)
from ilastik.classifiers.ilp_pixel_classifier import IlpVigraPixelClassifier

from ilastik.features.feature_extractor import FeatureExtractor, FeatureDataMismatchException
from ilastik.features.fastfilters import (
    GaussianSmoothing,
    HessianOfGaussianEigenvalues,
    GaussianGradientMagnitude,
    LaplacianOfGaussian,
    DifferenceOfGaussians,
    StructureTensorEigenvalues,
)
from ilastik.utility import flatten, unflatten, listify

parser = argparse.ArgumentParser(description="Runs ilastik prediction web server")
parser.add_argument("--host", default="localhost", help="ip or hostname where the server will listen")
parser.add_argument("--port", default=5000, type=int, help="port to listen on")
parser.add_argument("--ngurl", default="http://localhost:8080", help="url where neuroglancer is being served")
parser.add_argument("--sample-dirs", type=Path, help="List of directories containing n5 samples", nargs="+")
parser.add_argument(
    "--sample-tile-size", type=int, help="Force samples to use this tiles with this size (clamped to full image)"
)
args = parser.parse_args()

datasource_classes = [DataSource, SequenceDataSource]

feature_extractor_classes = [
    FeatureExtractor,  # this allows one to GET /feature_extractor and get a list of all created feature extractors
    GaussianSmoothing,
    HessianOfGaussianEigenvalues,
    GaussianGradientMagnitude,
    LaplacianOfGaussian,
    DifferenceOfGaussians,
    StructureTensorEigenvalues,
]

classifier_classes = [
    PixelClassifier,
    VigraPixelClassifier,
    ScikitLearnPixelClassifier,
    IlpVigraPixelClassifier,
    Annotation,
]

workflow_classes = {
    klass.__name__: klass for klass in datasource_classes + feature_extractor_classes + classifier_classes
}

app = Flask("WebserverHack")
CORS(app)


class Context:
    objects = {}

    @classmethod
    def do_rpc(cls):
        request_payload = cls.get_request_payload()
        obj = cls.load(request_payload.pop("self"))

    @classmethod
    def get_class_named(cls, name: str):
        name = name if name in workflow_classes else name.title().replace("_", "")
        try:
            return workflow_classes[name]
        except KeyError as e:
            import pydevd

            pydevd.settrace()
            print("asdads")

    @classmethod
    def create(cls, klass):
        request_payload = cls.get_request_payload()
        obj = klass.from_json_data(request_payload)
        key = cls.store(request_payload.get("id"), obj)
        return obj, key

    @classmethod
    def load(cls, key):
        return cls.objects[key]

    @classmethod
    def store(cls, obj_id: Optional[Hashable], obj):
        obj_id = obj_id if obj_id is not None else uuid.uuid4()
        key = f"pointer@{obj_id}"
        cls.objects[key] = obj
        return key

    @classmethod
    def remove(cls, klass: type, key):
        target_class = cls.objects[key].__class__
        if not issubclass(target_class, klass):
            raise Exception(f"Unexpected class {target_class} when deleting object with key {key}")
        return cls.objects.pop(key)

    @classmethod
    def get_request_payload(cls):
        payload = {}
        for k, v in request.form.items():
            if isinstance(v, str) and v.startswith("pointer@"):
                payload[k] = cls.load(v)
            else:
                payload[k] = v
        for k, v in request.files.items():
            payload[k] = v.read()
        return listify(unflatten(payload))

    @classmethod
    def get_all(cls, klass) -> Dict[str, object]:
        return {key: obj for key, obj in cls.objects.items() if isinstance(obj, klass)}


def do_predictions(roi: Slice5D, classifier_id: str, datasource_id: str) -> Predictions:
    classifier = Context.load(classifier_id)
    datasource = Context.load(datasource_id)
    backed_roi = DataSourceSlice(datasource, **roi.to_dict()).defined()

    predictions = classifier.allocate_predictions(backed_roi)
    with ThreadPoolExecutor() as executor:
        for raw_tile in backed_roi.get_tiles():

            def predict_tile(tile):
                tile_prediction = classifier.predict(tile)
                predictions.set(tile_prediction, autocrop=True)

            executor.submit(predict_tile, raw_tile)
    return predictions


@app.route("/predict/", methods=["GET"])
def predict():
    roi_params = {}
    for axis, v in request.args.items():
        if axis in "tcxyz":
            start, stop = [int(part) for part in v.split("_")]
            roi_params[axis] = slice(start, stop)

    predictions = do_predictions(
        roi=Slice5D(**roi_params),
        classifier_id=request.args["pixel_classifier_id"],
        datasource_id=request.args["data_source_id"],
    )

    channel = int(request.args.get("channel", 0))
    data = predictions.cut(Slice5D(c=channel)).as_uint8(normalized=True).raw("yx")
    out_image = PilImage.fromarray(data)
    out_file = io.BytesIO()
    out_image.save(out_file, "png")
    out_file.seek(0)
    return send_file(out_file, mimetype="image/png")


# https://github.com/google/neuroglancer/tree/master/src/neuroglancer/datasource/precomputed#unsharded-chunk-storage
@app.route(
    "/predictions/<classifier_id>/<datasource_id>/data/<int:xBegin>-<int:xEnd>_<int:yBegin>-<int:yEnd>_<int:zBegin>-<int:zEnd>"
)
def ng_predict(
    classifier_id: str, datasource_id: str, xBegin: int, xEnd: int, yBegin: int, yEnd: int, zBegin: int, zEnd: int
):
    requested_roi = Slice5D(x=slice(xBegin, xEnd), y=slice(yBegin, yEnd), z=slice(zBegin, zEnd))
    predictions = do_predictions(roi=requested_roi, classifier_id=classifier_id, datasource_id=datasource_id)

    # https://github.com/google/neuroglancer/tree/master/src/neuroglancer/datasource/precomputed#raw-chunk-encoding
    # "(...) data for the chunk is stored directly in little-endian binary format in [x, y, z, channel] Fortran order"
    resp = flask.make_response(predictions.as_uint8().raw("xyzc").tobytes("F"))
    resp.headers["Content-Type"] = "application/octet-stream"
    return resp


@app.route("/predictions/<classifier_id>/<datasource_id>/info/")
def info_dict(classifier_id: str, datasource_id: str) -> Dict:
    classifier = Context.load(classifier_id)
    datasource = Context.load(datasource_id)

    expected_predictions_shape = classifier.get_expected_roi(datasource.roi).shape

    resp = flask.jsonify(
        {
            "@type": "neuroglancer_multiscale_volume",
            "type": "image",
            "data_type": "uint8",  # DONT FORGET TO CONVERT PREDICTIONS TO UINT8!
            "num_channels": int(expected_predictions_shape.c),
            "scales": [
                {
                    "key": "data",
                    "size": [int(v) for v in expected_predictions_shape.to_tuple("xyz")],
                    "resolution": [1, 1, 1],
                    "voxel_offset": [0, 0, 0],
                    "chunk_sizes": [datasource.tile_shape.to_tuple("xyz")],
                    "encoding": "raw",
                }
            ],
        }
    )
    return resp


@app.route("/datasource/<datasource_id>/data/<int:xBegin>-<int:xEnd>_<int:yBegin>-<int:yEnd>_<int:zBegin>-<int:zEnd>")
def ng_raw(datasource_id: str, xBegin: int, xEnd: int, yBegin: int, yEnd: int, zBegin: int, zEnd: int):
    requested_roi = Slice5D(x=slice(xBegin, xEnd), y=slice(yBegin, yEnd), z=slice(zBegin, zEnd))
    datasource = Context.load(datasource_id)
    data = datasource.retrieve(requested_roi)

    resp = flask.make_response(data.raw("xyzc").tobytes("F"))
    resp.headers["Content-Type"] = "application/octet-stream"
    return resp


def get_sample_datasets() -> List[Dict]:
    rgb_shader = """void main() {
      emitRGB(vec3(
        toNormalized(getDataValue(0)),
        toNormalized(getDataValue(1)),
        toNormalized(getDataValue(2))
      ));
    }
    """

    grayscale_shader = """void main() {
      emitGrayscale(toNormalized(getDataValue(0)));
    }
    """

    protocol = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host", args.host)
    port = "" if "X-Forwarded-Host" in request.headers else f":{args.port}"
    prefix = request.headers.get("X-Forwarded-Prefix", "/")

    links = []
    for datasource_id, datasource in Context.get_all(DataSource).items():
        url_data = {
            "layers": [
                {
                    "source": f"precomputed://{protocol}://{host}{port}{prefix}datasource/{datasource_id}",
                    "type": "image",
                    "blend": "default",
                    "shader": grayscale_shader if datasource.shape.c == 1 else rgb_shader,
                    "shaderControls": {},
                    "name": datasource.name,
                },
                {"type": "annotation", "annotations": [], "voxelSize": [1, 1, 1], "name": "annotation"},
            ],
            "navigation": {"zoomFactor": 1},
            "selectedLayer": {"layer": "annotation", "visible": True},
            "layout": "xy",
        }
        yield {"url": f"{args.ngurl}#!" + urllib.parse.quote(str(json.dumps(url_data))), "name": datasource.name}


@app.route("/datasets")
def get_datasets():
    return flask.jsonify(list(get_sample_datasets()))


@app.route("/neuroglancer-samples")
def ng_samples():
    link_tags = [f'<a href="{sample["url"]}">{sample["name"]}</a><br/>' for sample in get_sample_datasets()]
    links = "\n".join(link_tags)
    return f"""
        <html>
            <head>
                <meta charset="UTF-8">
                <link rel=icon href="https://www.ilastik.org/assets/ilastik-logo.png">
            </head>

            <body>
                {links}
            </body>
        </html>
    """


@app.route("/datasource/<datasource_id>/info")
def datasource_info_dict(datasource_id: str) -> Dict:
    datasource = Context.load(datasource_id)

    resp = flask.jsonify(
        {
            "@type": "neuroglancer_multiscale_volume",
            "type": "image",
            "data_type": "uint8",  # DONT FORGET TO CONVERT PREDICTIONS TO UINT8!
            "num_channels": int(datasource.shape.c),
            "scales": [
                {
                    "key": "data",
                    "size": [int(v) for v in datasource.shape.to_tuple("xyz")],
                    "resolution": [1, 1, 1],
                    "voxel_offset": [0, 0, 0],
                    "chunk_sizes": [datasource.tile_shape.to_tuple("xyz")],
                    "encoding": "raw",
                }
            ],
        }
    )
    return resp


@app.route("/<class_name>/<object_id>", methods=["DELETE"])
def remove_object(class_name, object_id: str):
    Context.remove(Context.get_class_named(class_name), object_id)
    return flask.jsonify({"id": object_id})


@app.errorhandler(FeatureDataMismatchException)
def handle_feature_data_mismatch(error):
    return flask.Response(str(error), status=400)


@app.route("/<class_name>/", methods=["POST"])
def create_object(class_name: str):
    #    if Context.get_class_named(class_name) == Annotation:
    #        import pydevd; pydevd.settrace()

    obj, uid = Context.create(Context.get_class_named(class_name))
    if isinstance(obj, Annotation):  # DEBUG!!!!!!!!!!!!!!
        obj.as_uint8().show_channels()  # DEBUG!!!!!!!!!!!!!!!!!
    return json.dumps(uid)


@app.route("/<class_name>/", methods=["GET"])
def list_objects(class_name):
    klass = Context.get_class_named(class_name)
    return flask.Response(JsonSerializable.jsonify(Context.get_all(klass)), mimetype="application/json")


@app.route("/<class_name>/<object_id>", methods=["GET"])
def show_object(class_name: str, object_id: str):
    klass = Context.get_class_named(class_name)
    return flask.Response(Context.load(object_id).to_json(), mimetype="application/json")


for sample_dir in args.sample_dirs or ():
    for sample_file in sample_dir.iterdir():
        if sample_file.is_dir() and sample_file.suffix in (".n5", ".N5"):
            for dataset in sample_file.iterdir():
                if dataset.is_dir():
                    tile_shape = Shape5D.hypercube(args.sample_tile_size) if args.sample_tile_size else None
                    datasource = DataSource.create(dataset.absolute().as_posix(), tile_shape=tile_shape)
                    print(f"---->> Adding sample {datasource.name}")
                    Context.store(None, datasource)

app.run(host=args.host, port=args.port)
