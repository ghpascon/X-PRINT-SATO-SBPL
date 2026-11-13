from typing import Any
from pydantic import BaseModel, Field


class SetOrderData(BaseModel):
	customer_data: dict[str, Any] = Field(..., description='Selected customer data')
	production_order_data: dict[str, Any] = Field(..., description='Selected production order data')
	production_order_batch_data: dict[str, Any] = Field(
		..., description='Selected production order batch data'
	)
