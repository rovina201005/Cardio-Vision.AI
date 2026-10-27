from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException,
    Depends, status
)
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import SessionLocal, engine, Base
from models import Patient, Study, Prediction, User, AuditLog
from validation import validate_mri_file
from inference import predict_image, model
from auth import router as auth_router, get_current_user

Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="CardioVision AI API",
    description="Backend API for cardiac MRI research and myocarditis classification.",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".dcm", ".nii", ".nii.gz", ".jpg", ".jpeg", ".png"}
MODEL_READY = model is not None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def record_audit(
    session,
    user_id,
    action,
    entity_type,
    entity_id,
    details=None,
):
    session.add(
        AuditLog(
            id=str(uuid4()),
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            created_at=datetime.now(timezone.utc),
        )
    )


def get_accessible_patient(session, patient_id, user):
    patient = session.get(Patient, patient_id)

    if patient is None:
        raise HTTPException(
            status_code=404,
            detail="Patient not found.",
        )

    if user.role == "Admin":
        return patient

    if user.role != "Researcher":
        raise HTTPException(
            status_code=403,
            detail="Your role is not permitted to access patient records.",
        )

    if (
        patient.created_by_user_id != user.id
        and patient.assigned_to_user_id != user.id
    ):
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this patient.",
        )

    return patient


def get_accessible_study(session, study_id, user):
    study = session.get(Study, study_id)

    if study is None:
        raise HTTPException(
            status_code=404,
            detail="Study not found.",
        )

    get_accessible_patient(session, study.patient_id, user)
    return study


def latest_prediction(session, study_id):
    statement = (
        select(Prediction)
        .where(Prediction.study_id == study_id)
        .order_by(Prediction.predicted_at.desc().nulls_last())
    )
    return session.scalars(statement).first()


def study_to_dict(session, study):
    prediction = latest_prediction(session, study.study_id)

    return {
        "study_id": study.study_id,
        "patient_id": study.patient_id,
        "filename": study.filename,
        "stored_filename": study.stored_filename,
        "size_bytes": study.size_bytes,
        "format": study.format,
        "status": study.status,
        "validation": study.validation,
        "prediction_status": study.prediction_status,
        "prediction_result": prediction.result if prediction else None,
        "uploaded_at": study.uploaded_at.isoformat() if study.uploaded_at else None,
    }


@app.get("/")
def home():
    return {
        "message": "Welcome to CardioVision AI API",
        "status": "running",
    }


@app.get("/api/health")
def health_check():
    database_status = "connected"

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database_status = "disconnected"

    overall_status = (
        "healthy"
        if database_status == "connected" and MODEL_READY
        else "degraded"
    )

    return {
        "status": overall_status,
        "database_status": database_status,
        "model_status": "MODEL_LOADED" if MODEL_READY else "MODEL_NOT_READY",
        "message": (
            "PostgreSQL database and trained model are loaded."
            if overall_status == "healthy"
            else "Check database connectivity and model availability."
        ),
    }


@app.post("/api/upload")
async def upload_mri(
    patient_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    patient_id = patient_id.strip()

    if not patient_id:
        raise HTTPException(
            status_code=400,
            detail="Patient ID is required.",
        )

    original_name = Path(file.filename or "").name

    if not original_name:
        raise HTTPException(
            status_code=400,
            detail="A file is required.",
        )

    extension = (
        ".nii.gz"
        if original_name.lower().endswith(".nii.gz")
        else Path(original_name).suffix.lower()
    )

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Allowed formats: DICOM, NIfTI, JPG and PNG.",
        )

    study_id = str(uuid4())
    safe_filename = f"{study_id}{extension}"
    destination = UPLOAD_DIR / safe_filename
    size = 0

    try:
        with destination.open("wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="File exceeds the 100 MB limit.",
                    )

                buffer.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        validation = validate_mri_file(
            str(destination),
            original_name,
        )

        with SessionLocal() as session:
            with session.begin():
                patient = get_accessible_patient(session, patient_id, user)

                study = Study(
                    study_id=study_id,
                    patient_id=patient_id,
                    filename=original_name,
                    stored_filename=safe_filename,
                    size_bytes=size,
                    format=extension,
                    status=validation["validation_status"],
                    validation=validation,
                    prediction_status="NOT_RUN",
                    uploaded_at=datetime.now(timezone.utc),
                    uploaded_by_user_id=user.id,
                )

                session.add(study)

                record_audit(
                    session,
                    user.id,
                    "MRI_UPLOADED",
                    "Study",
                    study_id,
                    {"patient_id": patient_id, "filename": original_name},
                )

                session.flush()
                response = study_to_dict(session, study)

        return {
            **response,
            "message": (
                "File uploaded and basic format validation completed. "
                "Cardiac anatomy has not been verified."
            ),
        }

    except HTTPException:
        destination.unlink(missing_ok=True)
        raise

    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail="Upload or database operation failed.",
        )

    finally:
        await file.close()


@app.get("/api/patients/{patient_id}/studies")
def get_patient_studies(
    patient_id: str,
    user: User = Depends(get_current_user),
):
    with SessionLocal() as session:
        get_accessible_patient(session, patient_id, user)

        statement = (
            select(Study)
            .where(Study.patient_id == patient_id)
            .order_by(Study.uploaded_at.desc().nulls_last())
        )

        studies = session.scalars(statement).all()
        results = [
            study_to_dict(session, study)
            for study in studies
        ]

    return {
        "patient_id": patient_id,
        "total_studies": len(results),
        "studies": results,
    }


@app.get("/api/studies/{study_id}")
def get_study(
    study_id: str,
    user: User = Depends(get_current_user),
):
    with SessionLocal() as session:
        study = get_accessible_study(session, study_id, user)
        return study_to_dict(session, study)


@app.get("/api/studies/{study_id}/file")
def get_study_file(
    study_id: str,
    user: User = Depends(get_current_user),
):
    with SessionLocal() as session:
        study = get_accessible_study(session, study_id, user)

        safe_filename = Path(study.stored_filename).name
        original_filename = study.filename

    file_path = UPLOAD_DIR / safe_filename

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Study file not found.",
        )

    return FileResponse(
        path=file_path,
        filename=original_filename,
    )


@app.get("/api/studies")
def get_all_studies(
    user: User = Depends(get_current_user),
):
    with SessionLocal() as session:
        statement = select(Study).order_by(
            Study.uploaded_at.desc().nulls_last()
        )

        if user.role == "Researcher":
            statement = statement.join(
                Patient,
                Study.patient_id == Patient.id,
            ).where(
                (Patient.created_by_user_id == user.id)
                | (Patient.assigned_to_user_id == user.id)
            )

        elif user.role != "Admin":
            raise HTTPException(
                status_code=403,
                detail="Your role is not permitted to access studies.",
            )

        studies = session.scalars(statement).all()

        results = [
            study_to_dict(session, study)
            for study in studies
        ]

    return {
        "total_studies": len(results),
        "studies": results,
    }


@app.post("/api/studies/{study_id}/predict")
async def predict_study(
    study_id: str,
    user: User = Depends(get_current_user),
):
    if not MODEL_READY:
        raise HTTPException(
            status_code=503,
            detail="Trained model is not available.",
        )

    with SessionLocal() as session:
        study = get_accessible_study(session, study_id, user)

        safe_filename = Path(study.stored_filename).name
        file_path = UPLOAD_DIR / safe_filename
        patient_id = study.patient_id
        image_format = study.format

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Study file not found.",
        )

    if image_format not in {".jpg", ".jpeg", ".png"}:
        raise HTTPException(
            status_code=400,
            detail="Prediction currently supports JPEG and PNG images only.",
        )

    try:
        result = predict_image(str(file_path))

        prediction_record = {
            **result,
            "predicted_at": datetime.now(timezone.utc).isoformat(),
        }

        with SessionLocal() as session:
            with session.begin():
                study = get_accessible_study(session, study_id, user)

                session.add(
                    Prediction(
                        id=str(uuid4()),
                        study_id=study_id,
                        result=prediction_record,
                        predicted_at=datetime.now(timezone.utc),
                        predicted_by_user_id=user.id,
                    )
                )

                study.prediction_status = "COMPLETED"

                record_audit(
                    session,
                    user.id,
                    "PREDICTION_COMPLETED",
                    "Study",
                    study_id,
                    {"patient_id": patient_id},
                )

        return {
            "study_id": study_id,
            "patient_id": patient_id,
            **prediction_record,
        }

    except HTTPException:
        raise

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Prediction failed.",
        )

from admin import router as admin_router
app.include_router(admin_router)

from patients import router as patients_router
app.include_router(patients_router)


