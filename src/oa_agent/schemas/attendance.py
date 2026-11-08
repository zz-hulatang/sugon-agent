"""异常考勤工具只接受业务字段，身份和时长以 OA 返回为准。"""
from datetime import time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AttendanceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(min_length=1, max_length=64, description="查询工具返回的记录标识")
    offset_type: str = Field(min_length=1, max_length=40, description="查询工具返回的抵消类别名称")
    remark: str = Field(default="", max_length=1000, description="真实的异常说明")
    actual_start: str = Field(default="", description="忘打卡/卡机故障的实际开始时间，HH:MM:SS")
    actual_end: str = Field(default="", description="忘打卡/卡机故障的实际结束时间，HH:MM:SS")
    device_location: str = Field(default="", max_length=200, description="卡机故障的位置")

    @field_validator("actual_start", "actual_end")
    @classmethod
    def validate_time(cls, value):
        if not value:
            return value
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None or parsed.microsecond or len(value) not in (5, 8):
            raise ValueError("时间应为 HH:MM 或 HH:MM:SS")
        return parsed.isoformat()

    @model_validator(mode="after")
    def validate_correction(self):
        if self.offset_type in {"忘打卡", "卡机故障"}:
            if not self.actual_start or not self.actual_end:
                raise ValueError("忘打卡或卡机故障必须填写真实开始和结束时间")
            if self.actual_start >= self.actual_end:
                raise ValueError("实际结束时间必须晚于开始时间")
        elif self.actual_start or self.actual_end or self.device_location:
            raise ValueError("该抵消类别不接受实际时间或故障位置")
        if self.offset_type == "卡机故障" and not self.device_location.strip():
            raise ValueError("卡机故障必须填写故障位置")
        return self
