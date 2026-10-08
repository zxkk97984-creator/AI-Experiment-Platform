from pydantic import BaseModel


class AssignmentQuestionProgress(BaseModel):
    question_id: int
    latest_submission_id: int | None
    status: str | None
    completed: bool


class AssignmentProgressRead(BaseModel):
    items: list[AssignmentQuestionProgress]
