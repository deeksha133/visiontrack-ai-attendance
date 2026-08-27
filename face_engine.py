import base64
import json
import os
from pathlib import Path

import cv2
import numpy as np


class FaceEngineError(Exception): pass


class FaceEngine:
    def __init__(self, data_dir, model_path, labels_path):
        self.data_dir = Path(data_dir); self.model_path = Path(model_path); self.labels_path = Path(labels_path)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    @staticmethod
    def decode(data_url):
        try:
            encoded = data_url.split(",", 1)[-1]
            image = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), np.uint8), cv2.IMREAD_COLOR)
            if image is None: raise ValueError
            return image
        except Exception as exc:
            raise FaceEngineError("Invalid camera image received.") from exc

    def largest_face(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = self.detector.detectMultiScale(gray, 1.2, 5, minSize=(80,80))
        if len(faces) == 0: raise FaceEngineError("No face detected. Face the camera in good lighting.")
        x,y,w,h = max(faces, key=lambda f:f[2]*f[3])
        return cv2.resize(gray[y:y+h, x:x+w], (200,200)), (x+w/2, y+h/2, w*h)

    def register(self, student_id, frames):
        folder = self.data_dir / str(student_id); folder.mkdir(exist_ok=True)
        saved = 0
        for raw in frames:
            try:
                crop,_ = self.largest_face(self.decode(raw))
                cv2.imwrite(str(folder/f"{saved:02d}.jpg"), crop); saved += 1
            except FaceEngineError: continue
        if saved < 3: raise FaceEngineError("Not enough clear face samples. Try again with better lighting.")
        return saved

    def train(self):
        images, labels, label_map = [], [], {}
        for folder in self.data_dir.iterdir():
            if not folder.is_dir() or not folder.name.isdigit(): continue
            label = len(label_map); label_map[str(label)] = int(folder.name)
            for image_path in folder.glob("*.jpg"):
                image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if image is not None: images.append(image); labels.append(label)
        if not images: raise FaceEngineError("No face samples are available for training.")
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.train(images, np.array(labels)); recognizer.write(str(self.model_path))
        self.labels_path.write_text(json.dumps(label_map))

    def recognize(self, raw_frames):
        if not self.model_path.exists() or not self.labels_path.exists():
            raise FaceEngineError("Recognition model is not trained. Register a student first.")
        crops, centers, full_frames = [], [], []
        for raw in raw_frames:
            try:
                frame = self.decode(raw); crop,meta = self.largest_face(frame)
                crops.append(crop); centers.append(meta[:2]); full_frames.append(cv2.resize(frame,(320,240)))
            except FaceEngineError: continue
        if len(crops) < 3: raise FaceEngineError("Face was not visible in enough frames.")

        movement = max(x for x,_ in centers)-min(x for x,_ in centers) + max(y for _,y in centers)-min(y for _,y in centers)
        differences = [np.mean(cv2.absdiff(full_frames[i-1], full_frames[i])) for i in range(1,len(full_frames))]
        liveness = min(100.0, movement/2 + np.mean(differences)*3)
        if liveness < 8:
            raise FaceEngineError("Liveness check failed. Slowly turn your head and try again.")

        recognizer = cv2.face.LBPHFaceRecognizer_create(); recognizer.read(str(self.model_path))
        labels = json.loads(self.labels_path.read_text()); predictions = [recognizer.predict(c) for c in crops]
        best_label = min(set(p[0] for p in predictions), key=lambda label: np.mean([d for l,d in predictions if l==label]))
        distance = np.mean([d for l,d in predictions if l==best_label])
        confidence = max(0.0, min(100.0, 100-distance))
        if distance > 85: raise FaceEngineError("Face not recognized. Please register or try in better lighting.")
        return {"student_id": labels[str(best_label)], "confidence": round(confidence,1), "liveness_score": round(liveness,1)}
