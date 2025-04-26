from fastapi import APIRouter, Depends, HTTPException, status, Header, File, UploadFile, Body
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from lenny.models import get_db, session_scope 
from lenny.models.items import Item
from lenny.core.admin import verify_librarian
from lenny.core.items import delete_item, update_item_access, batch_process_books, process_book_file
from lenny.schemas.item import Item as ItemSchema, ItemDeleteResponse, ItemUpdate as ItemUpdateSchema
import os
import shutil
import re
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

router = APIRouter()

def get_session():
    return next(get_db())

@router.get('/', status_code=status.HTTP_200_OK)
async def root():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Lenny API</title>
    </head>
    <body>
        <h1 style="text-align: center;">Lenny: A Free, Open Source Lending System for Libraries</h1>
        <img src="/static/lenny.png" alt="Lenny Logo" style="display: block; margin: 0 auto;">
        <p style="text-align: center;">You can download & deploy it from <a href="https://github.com/ArchiveLabs/lenny">Github</a> </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content, media_type="text/html")

@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_or_batch_create_items(
    files: List[UploadFile] = File(...),
    s3_access_key: str = Header(...),
    s3_secret_key: str = Header(...),
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    if len(files) == 1:
        file = files[0]
        original_basename = os.path.basename(file.filename).strip()
        safe_filename = re.sub(r'\s+', '_', original_basename)
        safe_filename_lower = safe_filename.lower()

        if not (safe_filename_lower.endswith('.pdf') or safe_filename_lower.endswith('.epub')):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only PDF and EPUB file formats are supported."
            )

        temp_path = f"/tmp/{safe_filename}"
        item_result: Optional[Tuple[Optional[Item], str] | Item] = None

        try:
            if os.path.exists(temp_path):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Temporary file for {safe_filename} already exists. Please try again.")

            with open(temp_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            with session_scope() as session:
                item_result = process_book_file(session, temp_path, original_filename=safe_filename)

            if isinstance(item_result, tuple) and len(item_result) == 2:
                item_obj, status_msg = item_result
                if status_msg == "existing" and isinstance(item_obj, Item):
                    return JSONResponse(
                        status_code=status.HTTP_409_CONFLICT,
                        content={
                            "detail": f"Item with identifier '{item_obj.identifier}' already exists.",
                            "item": ItemSchema.model_validate(item_obj).model_dump()
                        }
                    )
                elif status_msg == "created" and isinstance(item_obj, Item):
                    return ItemSchema.model_validate(item_obj)
                elif status_msg.startswith("error_"):
                    logger.warning(f"Processing failed for {safe_filename}: {status_msg}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to process file {safe_filename}. Reason: {status_msg}"
                    )
                else:
                    logger.error(f"Unexpected tuple result from process_book_file for {safe_filename}: {item_result}")
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                       detail=f"Internal error processing file {safe_filename} after processing.")

            elif isinstance(item_result, Item):
                item_obj = item_result
                return ItemSchema.model_validate(item_obj)

            else:
                logger.error(f"Unexpected result type from process_book_file for {safe_filename}: {type(item_result)}")
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                   detail=f"Internal error processing file {safe_filename}. Unexpected return value.")

        except HTTPException as http_exc:
            raise http_exc
        except Exception as e:
            logger.exception(f"Unexpected error processing single file {safe_filename}: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                               detail=f"An unexpected internal server error occurred while processing {safe_filename}.")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception as remove_err:
                    logger.error(f"Failed to remove temporary file {temp_path}: {remove_err}")
            if hasattr(file, 'file') and file.file and not file.file.closed:
                file.file.close()

    elif len(files) > 1:
        temp_paths_map = {}
        batch_api_results = {"processed": [], "existing": [], "errors": []}
        paths_to_process = []

        for file in files:
            original_basename = os.path.basename(file.filename).strip()
            safe_filename = re.sub(r'\s+', '_', original_basename)
            safe_filename_lower = safe_filename.lower()

            if not (safe_filename_lower.endswith('.pdf') or safe_filename_lower.endswith('.epub')):
                batch_api_results["errors"].append({"filename": file.filename, "detail": "Unsupported file type"})
                continue

            temp_path = f"/tmp/{safe_filename}"

            try:
                if os.path.exists(temp_path):
                    batch_api_results["errors"].append({"filename": file.filename, "detail": f"Temporary file conflict for {safe_filename}. Skipped."})
                    continue

                with open(temp_path, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                temp_paths_map[temp_path] = file.filename
                paths_to_process.append(temp_path)
            except Exception as e:
                batch_api_results["errors"].append({"filename": file.filename, "detail": f"Failed to save temporary file: {e}"})
            finally:
                if hasattr(file, 'file') and file.file and not file.file.closed:
                    file.file.close()

        if not paths_to_process and not batch_api_results["errors"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid files provided for batch processing.")

        if paths_to_process:
            try:
                batch_results = batch_process_books(session_scope, paths_to_process)

                for success_item in batch_results["success"]:
                    temp_filename = os.path.basename(success_item["path"])
                    original_filename = temp_paths_map.get(success_item["path"], temp_filename)
                    batch_api_results["processed"].append({
                        "filename": original_filename,
                        "identifier": success_item["identifier"],
                        "title": success_item["title"],
                        "status": "created"
                    })

                for existing_item in batch_results["existing"]:
                    temp_filename = os.path.basename(existing_item["path"])
                    original_filename = temp_paths_map.get(existing_item["path"], temp_filename)
                    batch_api_results["existing"].append({
                        "filename": original_filename,
                        "identifier": existing_item["identifier"],
                        "title": existing_item["title"],
                        "status": "existing"
                    })

                for failed_item in batch_results["failed"]:
                    temp_filename = os.path.basename(failed_item["path"])
                    original_filename = temp_paths_map.get(failed_item["path"], temp_filename)
                    batch_api_results["errors"].append({
                        "filename": original_filename,
                        "detail": failed_item["reason"]
                    })

            except Exception as e:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                   detail=f"Batch processing error: {str(e)}")
            finally:
                for temp_path in paths_to_process:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

        if batch_api_results["processed"] or batch_api_results["existing"]:
            return JSONResponse(status_code=status.HTTP_207_MULTI_STATUS, content=batch_api_results)
        elif batch_api_results["errors"]:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=batch_api_results)

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided."
        )

@router.get("/items/{identifier}", response_model=ItemSchema)
def get_item_endpoint(identifier: str, db: Session = Depends(get_session)):
    item = db.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item

@router.patch("/items/{identifier}", response_model=ItemSchema)
def update_item_endpoint(
    identifier: str,
    item_update_data: ItemUpdateSchema = Body(...),
    s3_access_key: str = Header(...),
    s3_secret_key: str = Header(...),
    db: Session = Depends(get_session),
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    item = db.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    update_data = item_update_data.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided")

    item_status_changed = 'item_status' in update_data and update_data['item_status'] != item.item_status

    updated = False
    for key, value in update_data.items():
        if hasattr(item, key):
            setattr(item, key, value)
            updated = True

    if updated:
        try:
            db.commit()
            db.refresh(item)
            if item_status_changed:
                try:
                    update_item_access(session=db, identifier=identifier)
                except Exception as s3_error:
                    pass
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {str(e)}")

    return item

@router.delete("/items/{identifier}", response_model=ItemDeleteResponse)
def delete_item_endpoint(
    identifier: str,
    s3_access_key: str = Header(...),
    s3_secret_key: str = Header(...),
    db: Session = Depends(get_session),
):
    if not verify_librarian(s3_access_key, s3_secret_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid S3 credentials")

    try:
        item = db.query(Item).filter(Item.identifier == identifier).first()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        
        item_info = {
            "identifier": item.identifier,
            "title": item.title,
            "status": "deleted"
        }
        
        if not delete_item(db, identifier):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        
        return JSONResponse(content=item_info)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))