"""Jinja template compiler for cloud-init and other template-based configurations.

This module provides a clean, reusable way to compile Jinja templates
with context data. It's designed to be extensible for various use cases
like cloud-init data, hostname generation, SSH keys, passwords, etc.
"""

import logging
from typing import Optional

from jinja2 import Environment, TemplateError, select_autoescape

logger = logging.getLogger(__name__)


class TemplateCompilationError(Exception):
    """Exception raised when template compilation fails."""

    def __init__(
        self, message: str, template_name: Optional[str] = None, cause: Optional[Exception] = None
    ):
        """Initialize TemplateCompilationError.

        Args:
            message: Error message
            template_name: Name of the template that failed (optional)
            cause: Underlying exception (optional)
        """
        super().__init__(message)
        self.template_name = template_name
        self.cause = cause


class TemplateCompiler:
    """Compiler for Jinja templates.

    This class provides a clean interface for compiling Jinja templates
    with context data. It handles template detection, compilation, and error handling.
    """

    def __init__(
        self, enable_autoescape: bool = True, trim_blocks: bool = True, lstrip_blocks: bool = True
    ):
        """Initialize the TemplateCompiler.

        Args:
            enable_autoescape: Enable auto-escaping for security
            trim_blocks: Trim whitespace from block tags
            lstrip_blocks: Strip whitespace from block start tags

        """
        # Note: We ensure all template context keys always exist (even if None)
        # This prevents Jinja2 errors when accessing optional variables like vm_network.ipv4_address
        self.env = Environment(
            autoescape=select_autoescape(["html", "xml"]) if enable_autoescape else False,
            trim_blocks=trim_blocks,
            lstrip_blocks=lstrip_blocks,
        )

    def is_template(self, content: str) -> bool:
        """Check if content appears to be a Jinja template.

        This is a simple heuristic that checks for common Jinja syntax.
        More sophisticated detection can be added if needed.

        Args:
            content: Content to check

        Returns:
            True if content appears to be a Jinja template, False otherwise
        """
        if not content:
            return False

        # Check for common Jinja syntax patterns
        # We check for {{ and {% which are definitive Jinja markers
        jinja_patterns = [
            "{{",  # Variable interpolation
            "{%",  # Block tags
            "{#",  # Comments
        ]

        # Check if any definitive pattern appears in the content
        for pattern in jinja_patterns:
            if pattern in content:
                return True

        return False

    def compile(
        self, template_content: str, context: dict, template_name: Optional[str] = None
    ) -> str:
        """Compile a Jinja template with the given context.

        Args:
            template_content: Jinja template content as string
            context: Dictionary of context data for template rendering
            template_name: Optional name for the template (for error messages)

        Returns:
            Compiled template as string

        Raises:
            TemplateCompilationError: If template compilation fails
        """
        if not template_content:
            raise TemplateCompilationError("Template content is empty", template_name=template_name)

        try:
            # Create template from string
            template = self.env.from_string(template_content)

            # Render template with context
            rendered = template.render(**context)

            logger.debug(
                "Successfully compiled template%s", f" '{template_name}'" if template_name else ""
            )

            return rendered

        except TemplateError as e:
            error_msg = f"Template compilation failed: {str(e)}"
            if template_name:
                error_msg = f"Template '{template_name}' compilation failed: {str(e)}"

            logger.error(error_msg)
            raise TemplateCompilationError(error_msg, template_name=template_name, cause=e) from e

        except Exception as e:
            error_msg = f"Unexpected error during template compilation: {str(e)}"
            if template_name:
                error_msg = f"Unexpected error compiling template '{template_name}': {str(e)}"

            logger.error(error_msg, exc_info=True)
            raise TemplateCompilationError(error_msg, template_name=template_name, cause=e) from e

    def compile_if_template(
        self, content: str, context: dict, template_name: Optional[str] = None
    ) -> str:
        """Compile content only if it appears to be a Jinja template.

        If the content is not a template, it's returned as-is.
        This is useful for backward compatibility with non-template content.

        Args:
            content: Content that may or may not be a Jinja template
            context: Dictionary of context data for template rendering
            template_name: Optional name for the template (for error messages)

        Returns:
            Compiled template if it was a template, original content otherwise
        """
        if not self.is_template(content):
            logger.debug(
                "Content%s does not appear to be a template, returning as-is",
                f" '{template_name}'" if template_name else "",
            )
            return content

        return self.compile(content, context, template_name=template_name)


def compile_if_template(content: str, context: dict, template_name: Optional[str] = None) -> str:
    """Convenience function to compile content only if it's a template.

    Args:
        content: Content that may or may not be a Jinja template
        context: Dictionary of context data for template rendering
        template_name: Optional name for the template (for error messages)

    Returns:
        Compiled template if it was a template, original content otherwise
    """
    compiler = TemplateCompiler()
    return compiler.compile_if_template(content, context, template_name=template_name)
