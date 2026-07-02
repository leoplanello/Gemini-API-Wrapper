# Ordered list of candidate selectors to use when looking for the file input
# trigger button. Each is tried independently with a short 2-second timeout.
# Ordering: most specific / least likely to false-match first.
_ATTACH_BUTTON_SELECTORS = [
    # Explicit upload / attach labels (English & Portuguese)
    'button[aria-label="Upload file"]',
    'button[aria-label="Attach file"]',
    'button[aria-label="Adicionar arquivo"]',
    'button[aria-label="Add image"]',
    'button[aria-label="Adicionar imagem"]',
    'button[aria-label="Add photo"]',
    # Partial matches – kept deliberately narrow to avoid false positives
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
    # Generic icon buttons inside the prompt toolbar (last resort)
    '.input-area button[aria-label]',
    'rich-textarea button[aria-label]',
]

# Selector for the text input box
_INPUT_SELECTOR = 'div[contenteditable="true"], textarea, [role="textbox"]'

# Selector for the send button
_SEND_BTN_SELECTOR = (
    'button[aria-label*="Send"], button[aria-label*="Enviar"], '
    'button.send-button, button[type="submit"]'
)

# Selector for model responses
_RESPONSE_SELECTORS_LIST = [
    "model-response",
    "message-content",
    ".response-container-content",
    ".markdown.markdown-main-panel",
    "[data-response-index]",
    ".model-response",
    '[role="log"] .model-response'
]
_RESPONSE_SELECTOR = ", ".join(_RESPONSE_SELECTORS_LIST)

# Sub-selectors for extracting clean content without header/footer garbage
_CONTENT_SUB_SELECTORS = [
    "message-content",
    ".message-content",
    ".markdown",
    ".response-container-content",
    ".markdown.markdown-main-panel",
]
