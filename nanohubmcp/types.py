"""
MCP Type definitions following the Model Context Protocol specification.
Compatible with Python 3.6+ and aligned with FastMCP API.
"""

from typing import Any, Dict, List, Optional, Union
from enum import Enum


class Role(str, Enum):
    """Role for prompt messages."""
    USER = "user"
    ASSISTANT = "assistant"


class Tool(object):
    """MCP Tool definition."""

    def __init__(
        self,
        name,  # type: str
        description="",  # type: str
        inputSchema=None,  # type: Optional[Dict[str, Any]]
        tags=None,  # type: Optional[set]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.name = name
        self.description = description
        self.inputSchema = inputSchema if inputSchema is not None else {
            "type": "object",
            "properties": {},
            "required": []
        }
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema
        }


class TextContent(object):
    """Text content for tool results."""

    def __init__(self, text="", type="text"):
        # type: (str, str) -> None
        self.type = type
        self.text = text

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {"type": self.type, "text": self.text}


class ImageContent(object):
    """Image content for tool results."""

    def __init__(self, data="", mimeType="image/png", type="image"):
        # type: (str, str, str) -> None
        self.type = type
        self.data = data  # base64 encoded
        self.mimeType = mimeType

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {"type": self.type, "data": self.data, "mimeType": self.mimeType}


class ToolResult(object):
    """
    Result of calling a tool. Aligned with FastMCP ToolResult.

    Args:
        content: Text content or list of content items
        is_error: Whether the result represents an error
        meta: Optional metadata dictionary
    """

    def __init__(
        self,
        content=None,  # type: Optional[Union[str, List[Union[TextContent, ImageContent]]]]
        is_error=False,  # type: bool
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        if content is None:
            self._content = []
        elif isinstance(content, str):
            self._content = [TextContent(text=content)]
        elif isinstance(content, list):
            self._content = content
        else:
            self._content = [TextContent(text=str(content))]

        self.is_error = is_error
        self.meta = meta or {}

    @property
    def content(self):
        # type: () -> List[Union[TextContent, ImageContent]]
        return self._content

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "content": [c.to_dict() for c in self._content],
            "isError": self.is_error
        }


# Backwards compatibility alias
CallToolResult = ToolResult


class Resource(object):
    """MCP Resource definition."""

    def __init__(
        self,
        uri,  # type: str
        name="",  # type: str
        description=None,  # type: Optional[str]
        mimeType=None,  # type: Optional[str]
        tags=None,  # type: Optional[set]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.uri = uri
        self.name = name or uri
        self.description = description
        self.mimeType = mimeType
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"uri": self.uri, "name": self.name}
        if self.description:
            result["description"] = self.description
        if self.mimeType:
            result["mimeType"] = self.mimeType
        return result


class ResourceContent(object):
    """Content returned when reading a resource."""

    def __init__(
        self,
        uri="",  # type: str
        content="",  # type: str
        text=None,  # type: Optional[str]
        blob=None,  # type: Optional[str]
        mime_type=None  # type: Optional[str]
    ):
        # type: (...) -> None
        self.uri = uri
        # Support both 'content' and 'text' for compatibility
        self.text = text if text is not None else content
        self.blob = blob  # base64 encoded
        self.mime_type = mime_type

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"uri": self.uri}
        if self.text is not None:
            result["text"] = self.text
        if self.blob is not None:
            result["blob"] = self.blob
        if self.mime_type:
            result["mimeType"] = self.mime_type
        return result


class ResourceResult(object):
    """
    Result of reading a resource. Aligned with FastMCP ResourceResult.

    Args:
        contents: List of ResourceContent or single content string
        meta: Optional metadata dictionary
    """

    def __init__(
        self,
        contents=None,  # type: Optional[Union[str, List[ResourceContent]]]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        if contents is None:
            self._contents = []
        elif isinstance(contents, str):
            self._contents = [ResourceContent(content=contents)]
        elif isinstance(contents, list):
            self._contents = contents
        else:
            self._contents = [ResourceContent(content=str(contents))]

        self.meta = meta or {}

    @property
    def contents(self):
        # type: () -> List[ResourceContent]
        return self._contents

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {"contents": [c.to_dict() for c in self._contents]}


# Backwards compatibility alias
ReadResourceResult = ResourceResult


class Message(object):
    """
    A message in a prompt. Aligned with FastMCP Message.

    Args:
        content: Message content (string or content object)
        role: Message role (user or assistant)
    """

    def __init__(
        self,
        content,  # type: Union[str, TextContent, ImageContent]
        role="user"  # type: str
    ):
        # type: (...) -> None
        if isinstance(content, str):
            self._content = TextContent(text=content)
        else:
            self._content = content

        self.role = role

    @property
    def content(self):
        # type: () -> Union[TextContent, ImageContent]
        return self._content

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "role": self.role,
            "content": self._content.to_dict()
        }


# Backwards compatibility alias
PromptMessage = Message


class Prompt(object):
    """MCP Prompt definition."""

    def __init__(
        self,
        name,  # type: str
        description=None,  # type: Optional[str]
        arguments=None,  # type: Optional[List[Dict[str, Any]]]
        tags=None,  # type: Optional[set]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.name = name
        self.description = description
        self.arguments = arguments if arguments is not None else []
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"name": self.name}
        if self.description:
            result["description"] = self.description
        if self.arguments:
            result["arguments"] = self.arguments
        return result


class PromptResult(object):
    """
    Result of getting a prompt. Aligned with FastMCP PromptResult.

    Args:
        messages: List of Message objects or strings
        description: Optional description
        meta: Optional metadata dictionary
    """

    def __init__(
        self,
        messages=None,  # type: Optional[List[Union[Message, str, Dict]]]
        description=None,  # type: Optional[str]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self._messages = []
        if messages:
            for msg in messages:
                if isinstance(msg, Message):
                    self._messages.append(msg)
                elif isinstance(msg, str):
                    self._messages.append(Message(msg))
                elif isinstance(msg, dict):
                    # Handle dict format {"role": "user", "content": "..."}
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if isinstance(content, dict):
                        content = content.get("text", str(content))
                    self._messages.append(Message(content, role=role))

        self.description = description
        self.meta = meta or {}

    @property
    def messages(self):
        # type: () -> List[Message]
        return self._messages

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"messages": [m.to_dict() for m in self._messages]}
        if self.description:
            result["description"] = self.description
        return result


# Backwards compatibility alias
GetPromptResult = PromptResult


class ServerCapabilities(object):
    """Server capabilities advertised during initialization."""

    def __init__(self, tools=False, resources=False, prompts=False, logging=False):
        # type: (bool, bool, bool, bool) -> None
        self.tools = tools
        self.resources = resources
        self.prompts = prompts
        self.logging = logging

    def to_dict(self):
        # type: () -> Dict[str, Any]
        caps = {}
        if self.tools:
            caps["tools"] = {}
        if self.resources:
            caps["resources"] = {}
        if self.prompts:
            caps["prompts"] = {}
        if self.logging:
            caps["logging"] = {}
        return caps


class ServerInfo(object):
    """Server information."""

    def __init__(self, name, version="1.0.0"):
        # type: (str, str) -> None
        self.name = name
        self.version = version

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {"name": self.name, "version": self.version}


class Image(object):
    """
    Image helper for returning images from tools.
    Aligned with FastMCP Image utility.
    """

    def __init__(
        self,
        data=None,  # type: Optional[str]
        path=None,  # type: Optional[str]
        mime_type="image/png"  # type: str
    ):
        # type: (...) -> None
        self._data = data
        self._path = path
        self.mime_type = mime_type

    def to_content(self):
        # type: () -> ImageContent
        """Convert to ImageContent for tool results."""
        import base64

        if self._data:
            data = self._data
        elif self._path:
            with open(self._path, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
        else:
            data = ""

        return ImageContent(data=data, mimeType=self.mime_type)
