"""
MCP Type definitions following the Model Context Protocol specification.
Compatible with Python 3.7+ and aligned with FastMCP API.
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
        meta=None,  # type: Optional[Dict[str, Any]]
        outputSchema=None,  # type: Optional[Dict[str, Any]]
        annotations=None,  # type: Optional[Dict[str, Any]]
        title=None  # type: Optional[str]
    ):
        # type: (...) -> None
        self.name = name
        # Human-readable display name. `annotations.title` is the older
        # place for this; a top-level `title` has precedence since
        # 2025-06-18, and clients fall back to the annotation.
        self.title = title
        self.description = description
        self.inputSchema = inputSchema if inputSchema is not None else {
            "type": "object",
            "properties": {},
            "required": []
        }
        self.tags = tags or set()
        self.meta = meta or {}
        self.outputSchema = outputSchema
        # MCP ToolAnnotations (readOnlyHint, destructiveHint, idempotentHint,
        # openWorldHint, title) — behavioral hints for clients, not guarantees.
        self.annotations = annotations

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema
        }
        if self.title:
            result["title"] = self.title
        if self.outputSchema:
            result["outputSchema"] = self.outputSchema
        if self.annotations:
            result["annotations"] = self.annotations
        if self.meta:
            result["_meta"] = self.meta
        return result


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


class AudioContent(object):
    """Audio content for tool results and prompt messages.

    Introduced in 2025-03-26. A client that negotiated 2024-11-05 has no
    schema for it and will reject the result, so only return one when the
    server's clients speak a later revision.
    """

    def __init__(self, data="", mimeType="audio/wav", type="audio"):
        # type: (str, str, str) -> None
        self.type = type
        self.data = data  # base64 encoded
        self.mimeType = mimeType

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {"type": self.type, "data": self.data, "mimeType": self.mimeType}


class ResourceLink(object):
    """A pointer to a resource, returned in place of its contents.

    "A tool MAY return links to Resources, to provide additional context or
    data." The client fetches or subscribes to the URI itself, which keeps a
    large payload out of the tool result.

    Introduced in 2025-06-18; see the note on :class:`AudioContent` about
    returning one to a 2024-11-05 client.
    """

    def __init__(
        self,
        uri,  # type: str
        name="",  # type: str
        description=None,  # type: Optional[str]
        mimeType=None,  # type: Optional[str]
        title=None,  # type: Optional[str]
        annotations=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.type = "resource_link"
        self.uri = uri
        self.name = name or uri
        self.title = title
        self.description = description
        self.mimeType = mimeType
        self.annotations = annotations

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"type": self.type, "uri": self.uri,
                  "name": self.name}  # type: Dict[str, Any]
        if self.title:
            result["title"] = self.title
        if self.description:
            result["description"] = self.description
        if self.mimeType:
            result["mimeType"] = self.mimeType
        if self.annotations:
            result["annotations"] = self.annotations
        return result


class EmbeddedResource(object):
    """A resource's contents carried inline in a result.

    The counterpart to :class:`ResourceLink`: the bytes travel with the
    result instead of the client fetching them.
    """

    def __init__(
        self,
        uri,  # type: str
        text=None,  # type: Optional[str]
        blob=None,  # type: Optional[str]
        mimeType=None,  # type: Optional[str]
        annotations=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.type = "resource"
        self.uri = uri
        self.text = text
        self.blob = blob
        self.mimeType = mimeType
        self.annotations = annotations

    def to_dict(self):
        # type: () -> Dict[str, Any]
        # The spec splits these into TextResourceContents (requires text)
        # and BlobResourceContents (requires blob); emitting both, or
        # neither, matches neither variant.
        resource = {"uri": self.uri}  # type: Dict[str, Any]
        if self.text is not None:
            resource["text"] = self.text
        elif self.blob is not None:
            resource["blob"] = self.blob
        else:
            resource["text"] = ""
        if self.mimeType:
            resource["mimeType"] = self.mimeType
        result = {"type": self.type,
                  "resource": resource}  # type: Dict[str, Any]
        if self.annotations:
            result["annotations"] = self.annotations
        return result


class ToolResult(object):
    """
    Result of calling a tool. Aligned with FastMCP ToolResult.

    Args:
        content: Text content or list of content items
        is_error: Whether the result represents an error
        meta: Optional metadata dictionary, emitted as `_meta`
        structured_content: Optional JSON value emitted as `structuredContent`.
            Required by the spec whenever the tool declares an `outputSchema`;
            for backwards compatibility the serialized JSON SHOULD also appear
            as a text content block, which this adds when `content` is empty.
    """

    _UNSET = object()

    def __init__(
        self,
        content=None,  # type: Optional[Union[str, List[Union[TextContent, ImageContent]]]]
        is_error=False,  # type: bool
        meta=None,  # type: Optional[Dict[str, Any]]
        structured_content=_UNSET  # type: Any
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
        self.structured_content = structured_content
        if structured_content is not ToolResult._UNSET and not self._content:
            # The spec's backwards-compatibility SHOULD: a client that does
            # not read `structuredContent` still has something to show.
            import json as _json
            self._content = [TextContent(text=_json.dumps(structured_content))]

    @property
    def has_structured_content(self):
        # type: () -> bool
        """Whether `structuredContent` was supplied (``None`` is a value)."""
        return self.structured_content is not ToolResult._UNSET

    @property
    def content(self):
        # type: () -> List[Union[TextContent, ImageContent]]
        return self._content

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {
            "content": [c.to_dict() for c in self._content],
            "isError": self.is_error
        }
        if self.has_structured_content:
            result["structuredContent"] = self.structured_content
        if self.meta:
            # Was accepted by __init__ and then dropped here, so every
            # `ToolResult(meta=...)` silently lost its metadata on the wire.
            result["_meta"] = self.meta
        return result


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
        meta=None,  # type: Optional[Dict[str, Any]]
        title=None,  # type: Optional[str]
        annotations=None,  # type: Optional[Dict[str, Any]]
        size=None  # type: Optional[int]
    ):
        # type: (...) -> None
        self.uri = uri
        self.name = name or uri
        # Display name, distinct from `name`, which is the programmatic id.
        self.title = title
        self.description = description
        self.mimeType = mimeType
        # audience / priority / lastModified hints for the client.
        self.annotations = annotations
        self.size = size
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"uri": self.uri, "name": self.name}  # type: Dict[str, Any]
        if self.title:
            result["title"] = self.title
        if self.description:
            result["description"] = self.description
        if self.mimeType:
            result["mimeType"] = self.mimeType
        if self.annotations:
            result["annotations"] = self.annotations
        if isinstance(self.size, int):
            result["size"] = self.size
        if self.meta:
            result["_meta"] = self.meta
        return result


class ResourceTemplate(object):
    """MCP resource template: a parameterized family of resources.

    The wire shape differs from :class:`Resource` in exactly one field —
    `uriTemplate` in place of `uri` — because a template names no single
    resource and so belongs in `resources/templates/list`, not
    `resources/list`.
    """

    def __init__(
        self,
        uriTemplate,  # type: str
        name="",  # type: str
        title=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        mimeType=None,  # type: Optional[str]
        annotations=None,  # type: Optional[Dict[str, Any]]
        tags=None,  # type: Optional[set]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self.uriTemplate = uriTemplate
        self.name = name or uriTemplate
        self.title = title
        self.description = description
        self.mimeType = mimeType
        self.annotations = annotations
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"uriTemplate": self.uriTemplate,
                  "name": self.name}  # type: Dict[str, Any]
        if self.title:
            result["title"] = self.title
        if self.description:
            result["description"] = self.description
        if self.mimeType:
            result["mimeType"] = self.mimeType
        if self.annotations:
            result["annotations"] = self.annotations
        if self.meta:
            result["_meta"] = self.meta
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
        # Support both 'content' and 'text' for compatibility.
        # A blob resource emits no `text` at all: the spec splits these into
        # TextResourceContents (requires text) and BlobResourceContents
        # (requires blob), so sending an empty string alongside a blob matches
        # neither variant cleanly and reads as empty text to a strict client.
        if text is not None:
            self.text = text
        elif content or blob is None:
            self.text = content
        else:
            self.text = None
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


class Skill(object):
    """
    MCP Skill entry (SEP-2640 Skills Extension).

    Returned by skills/list and skills/get: a skill's SKILL.md URI, its
    frontmatter verbatim, and a manifest of every file it serves.
    """

    def __init__(
        self,
        uri,  # type: str
        frontmatter,  # type: Dict[str, Any]
        resources  # type: Union[List[Dict[str, Any]], str]
    ):
        # type: (...) -> None
        self.uri = uri
        self.frontmatter = frontmatter
        # Either a list of {"uri", "digest", "size"} entries, or the literal
        # string "dynamic" for a skill whose content has no stable digests.
        self.resources = resources

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "uri": self.uri,
            "frontmatter": self.frontmatter,
            "resources": self.resources,
        }


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
        meta=None,  # type: Optional[Dict[str, Any]]
        title=None  # type: Optional[str]
    ):
        # type: (...) -> None
        self.name = name
        self.title = title
        self.description = description
        self.arguments = arguments if arguments is not None else []
        self.tags = tags or set()
        self.meta = meta or {}

    def to_dict(self):
        # type: () -> Dict[str, Any]
        result = {"name": self.name}
        if self.title:
            result["title"] = self.title
        if self.description:
            result["description"] = self.description
        if self.arguments:
            result["arguments"] = self.arguments
        if self.meta:
            result["_meta"] = self.meta
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

    def __init__(self, tools=False, resources=False, prompts=False, logging=False,
                 extensions=None, list_changed=False, subscribe=False):
        # type: (bool, bool, bool, bool, Optional[Dict[str, Any]], bool, bool) -> None
        self.tools = tools
        self.resources = resources
        self.prompts = prompts
        self.logging = logging
        self.extensions = extensions or {}
        # True once the server can gain or lose tools/resources/prompts after
        # start-up, which is what makes a listChanged notification meaningful.
        self.list_changed = list_changed
        # True when the server can emit notifications/resources/updated.
        self.subscribe = subscribe

    def to_dict(self, protocol_version=None):
        # type: (Optional[str]) -> Dict[str, Any]
        caps = {}
        # Only claim listChanged when the server will actually send one.
        changed = bool(self.list_changed)
        if self.tools:
            caps["tools"] = {"listChanged": changed}  # Not empty to ensure {} in JSON
        if self.resources:
            caps["resources"] = {"listChanged": changed,
                                 "subscribe": bool(self.subscribe)}
        if self.prompts:
            caps["prompts"] = {"listChanged": changed}
        if self.logging:
            # The logging capability carries no sub-fields: its presence is the
            # declaration. `listChanged` belongs to tools/resources/prompts.
            caps["logging"] = {}
        if self.extensions:
            # `extensions` is where 2026-07-28 put these. Earlier revisions
            # define `experimental` for exactly this purpose and know
            # nothing of `extensions`, so they are told in both places:
            # the spec's, and the one this server's own clients already
            # read. Both are additive — neither schema forbids the other.
            caps["extensions"] = self.extensions
            if protocol_version and protocol_version < "2026-07-28":
                caps["experimental"] = self.extensions
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


class InputRequired(BaseException):
    """Raised inside a handler when it needs input from the client.

    Under Multi Round-Trip Requests (2026-07-28), a server no longer pushes
    `elicitation/create`, `sampling/createMessage`, or `roots/list` down to the
    client and blocks. It returns an ``InputRequiredResult`` naming what it
    needs; the client answers by retrying the original request with
    ``inputResponses``. Handlers never raise this directly — ``ctx.elicit()``
    and friends raise it when the current request is speaking a revision that
    uses MRTR.

    Deliberately derived from ``BaseException``, not ``Exception``: it is a
    control-flow signal, and a handler with a broad ``except Exception`` around
    its body would otherwise swallow the ask and return a wrong answer instead
    of asking. ``finally`` blocks still run.

    Args:
        requests: map of server-assigned key -> JSON-RPC request object.
    """

    def __init__(self, requests):
        # type: (Dict[str, Any]) -> None
        super().__init__("client input required")
        self.requests = requests or {}
