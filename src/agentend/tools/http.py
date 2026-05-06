import httpx

from agentend.tools.base import ToolContext, ToolResult


class HttpRequestTool:
    name = "http.request"
    description = "Make an HTTP request and return response text."
    input_schema = {"type": "object", "required": ["url"]}
    allowed_methods = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        method = str(input_data.get("method", "GET")).upper()
        if method not in self.allowed_methods:
            raise ValueError(f"Unsupported HTTP method: {method}")
        response = httpx.request(method, str(input_data["url"]), json=input_data.get("json"), timeout=20)
        return ToolResult(
            content=response.text,
            data={"method": method, "status_code": response.status_code, "headers": dict(response.headers)},
        )
