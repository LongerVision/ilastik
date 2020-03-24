from typing import Type, Hashable, Optional, List

import flask

from ilastik.workflows.pixelClassification.pixel_classification_workflow_2 import PixelClassificationWorkflow2
from ilastik.server.WebContext import WebContext
from ilastik.classifiers.ilp_pixel_classifier import IlpVigraPixelClassifier
from ilastik.features.ilp_filter import IlpFilter
from ilastik.annotations import Annotation
from ilastik import Project


class PixelClassificationWorkflow2WebAdapter:
    def __init__(self, *, web_context: Type[WebContext], workflow: PixelClassificationWorkflow2):
        self.web_context = web_context
        self.workflow = workflow

    def _drop_classifier(self) -> None:
        classifier = self.workflow.drop_classifier()
        if classifier is not None:
            self.web_context.remove(self.classificer.__class__, self.classifier)

    def _store_classifier(self, classifier: Optional[IlpVigraPixelClassifier]) -> Optional[Hashable]:
        if classifier is not None:
            return self.web_context.store(classifier)
        else:
            return None

    def add_feature_extractors(self, extractors: List[IlpFilter], updateClassifier: bool = True) -> flask.Response:
        "Adds feature extractors to workflow, returns uuid of the extractors"
        self._store_classifier(self.workflow.add_feature_extractors(extractors, updateClassifier))
        return flask.jsonify([self.web_context.store(extractor) for extractor in extractors])

    def remove_feature_extractors(self, extractors: List[IlpFilter], updateClassifier: bool = True) -> flask.Response:
        classifier = self.workflow.remove_feature_extractors(extractors, updateClassifier)
        classifier_id = self._store_classifier(classifier)
        for extractor in extractors:
            self.web_context.remove(extractor)
        return flask.jsonify(classifier_id)

    def add_annotations(self, annotations: List[Annotation], updateClassifier: bool = True) -> flask.Response:
        "Adds annotations to workflow, returns uuid of the annotations"
        classifier = self.workflow.add_annotations(annotations, updateClassifier=updateClassifier)
        self._store_classifier(classifier)
        annotation_ids = [self.web_context.store(annotation) for annotation in annotations]
        return flask.jsonify(annotation_ids)

    def remove_annotations(self, annotations: List[Annotation], updateClassifier: bool = True) -> flask.Response:
        classifier = self.workflow.remove_annotations(annotations, updateClassifier=updateClassifier)
        classifier_id = self._store_classifier(classifier)
        return flask.jsonify(classifier_id)

    def get_classifier(self) -> flask.Response:
        if self.workflow.classifier is None:
            self.workflow.try_update_pixel_classifier(True)
        classifier_id = self._store_classifier(self.workflow.classifier)
        return flask.jsonify(classifier_id)

    def generate_ilp(self) -> bytes:
        project, backing_file = Project.from_ilp_data(self.workflow.ilp_data)
        project.close()
        backing_file.seek(0)
        return flask.send_file(backing_file, mimetype="application/octet-stream")
