ObjC.import("AppKit");

var TIMEOUT_MODAL_RESPONSE = 4242;
var WINDOW_LEVEL_GRACE_SECONDS = 0.5;
var TEXT_AREA_WIDTH = 640;
var MIN_PROMPT_AREA_HEIGHT = 150;
var MAX_PROMPT_AREA_HEIGHT = 520;
var PROMPT_SCREEN_HEIGHT_RATIO = 0.45;
var RESPONSE_AREA_HEIGHT = 180;
var PANE_GAP = 16;
var TOGGLE_ROW_HEIGHT = 44;
var TOGGLE_BUTTON_WIDTH = 116;
var TOGGLE_BUTTON_HEIGHT = 28;
var PROMPT_DOCUMENT_VERSION = 2;
var PROMPT_TEXT_INSET_Y = 0;
var CODE_BLOCK_PADDING = 6;
var CODE_BLOCK_CONTENT_WIDTH_PERCENT = 100;

var promptApplication = null;
var promptWindow = null;
var promptTextView = null;
var promptScrollView = null;
var promptViewportHeight = 0;
var renderedPromptContentHeight = 0;
var plainPromptContentHeight = 0;
var responseTextView = null;
var toggleButton = null;
var renderedPrompt = null;
var plainPrompt = null;
var showingRendered = true;
var timedOut = false;
var attentionRequest = 0;
var centeredAlignmentValue = null;

ObjC.registerSubclass({
    name: "AskHumanDialogController",
    superclass: "NSObject",
    methods: {
        "lowerWindow:": {
            types: ["void", ["id"]],
            implementation: function (_timer) {
                promptWindow.setLevel($.NSNormalWindowLevel);
                if (Boolean(ObjC.unwrap(promptApplication.active))) {
                    promptWindow.makeKeyAndOrderFront(null);
                } else {
                    attentionRequest = Number(
                        promptApplication.requestUserAttention($.NSInformationalRequest)
                    );
                }
            }
        },
        "timeOut:": {
            types: ["void", ["id"]],
            implementation: function (_timer) {
                timedOut = true;
                $.NSApp.stopModalWithCode(TIMEOUT_MODAL_RESPONSE);
            }
        },
        "togglePromptFormat:": {
            types: ["void", ["id"]],
            implementation: function (_sender) {
                showingRendered = !showingRendered;
                applyPromptValue(
                    showingRendered ? renderedPrompt : plainPrompt,
                    showingRendered
                        ? renderedPromptContentHeight
                        : plainPromptContentHeight
                );
                toggleButton.setTitle(showingRendered ? "Show Plain" : "Show Rendered");
                promptWindow.makeFirstResponder(responseTextView);
            }
        },
        "textView:clickedOnLink:atIndex:": {
            types: ["bool", ["id", "id", "NSUInteger"]],
            implementation: function (_textView, link, _characterIndex) {
                // Explicitly handle links because NSTextView's responder-chain fallback
                // is unreliable when the delegate is implemented through JXA.
                return openPromptLink(link);
            }
        }
    }
});

function openPromptLink(link) {
    return Boolean(ObjC.unwrap($.NSWorkspace.sharedWorkspace.openURL(link)));
}

function addMenuItem(menu, title, action, key, modifiers) {
    var item = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent(
        title,
        $.NSSelectorFromString(action),
        key
    );
    if (modifiers !== null) {
        item.setKeyEquivalentModifierMask(modifiers);
    }
    menu.addItem(item);
}

function installMainMenu(application, title) {
    var mainMenu = $.NSMenu.alloc.initWithTitle("");

    var applicationMenuItem = $.NSMenuItem.alloc.init;
    mainMenu.addItem(applicationMenuItem);
    var applicationMenu = $.NSMenu.alloc.initWithTitle(title);
    applicationMenuItem.setSubmenu(applicationMenu);
    addMenuItem(applicationMenu, "Quit " + title, "terminate:", "q", null);

    var editMenuItem = $.NSMenuItem.alloc.init;
    mainMenu.addItem(editMenuItem);
    var editMenu = $.NSMenu.alloc.initWithTitle("Edit");
    editMenuItem.setSubmenu(editMenu);
    addMenuItem(editMenu, "Undo", "undo:", "z", null);
    addMenuItem(
        editMenu,
        "Redo",
        "redo:",
        "z",
        $.NSEventModifierFlagCommand | $.NSEventModifierFlagShift
    );
    editMenu.addItem($.NSMenuItem.separatorItem);
    addMenuItem(editMenu, "Cut", "cut:", "x", null);
    addMenuItem(editMenu, "Copy", "copy:", "c", null);
    addMenuItem(editMenu, "Paste", "paste:", "v", null);
    addMenuItem(editMenu, "Select All", "selectAll:", "a", null);

    application.setMainMenu(mainMenu);
}

function readPromptDocument() {
    var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var jsonText = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding);
    var document = JSON.parse(ObjC.unwrap(jsonText));
    var fallbackText = typeof document.fallback_text === "string"
        ? document.fallback_text
        : "";
    if (
        Number(document.version) !== PROMPT_DOCUMENT_VERSION ||
        !promptBlocksAreValid(document.rendered_blocks) ||
        !promptBlocksAreValid(document.plain_blocks)
    ) {
        return {
            fallback_text: fallbackText,
            show_format_toggle: false,
            rendered_blocks: [plainPromptBlock(fallbackText)],
            plain_blocks: [plainPromptBlock(fallbackText)]
        };
    }
    return document;
}

function plainPromptBlock(text) {
    return {
        type: "paragraph",
        spans: [{text: text}],
        paragraph_break: true,
        blank_line_after: true
    };
}

function promptBlocksAreValid(blocks) {
    return Array.isArray(blocks) && blocks.every(function (block) {
        return block &&
            (block.type === "paragraph" || block.type === "code_block") &&
            Array.isArray(block.spans) &&
            block.spans.every(function (span) {
                return span && typeof span.text === "string";
            });
    });
}

function makeMutableAttributedString(text) {
    return $.NSMutableAttributedString.alloc.performSelectorWithObject(
        $.NSSelectorFromString("initWithString:"),
        $(text)
    );
}

function makeFallbackAttributedString(text) {
    // Keep this independent from document-font conversion so renderer failures
    // cannot recursively break the complete plain-text fallback.
    var value = makeMutableAttributedString(text);
    var length = String(text).length;
    if (length > 0) {
        value.addAttributeValueRange(
            $.NSFontAttributeName,
            $.NSFont.systemFontOfSize(Number(ObjC.unwrap($.NSFont.systemFontSize))),
            $.NSMakeRange(0, length)
        );
        value.addAttributeValueRange(
            $.NSForegroundColorAttributeName,
            $.NSColor.labelColor,
            $.NSMakeRange(0, length)
        );
    }
    return value;
}

function fontForSpan(span, baseSize) {
    var size = baseSize;
    if (span.heading === 1) {
        size += 7;
    } else if (span.heading === 2) {
        size += 4;
    } else if (span.heading === 3) {
        size += 2;
    }

    // AppKit document fonts let TextKit choose its normal Unicode fallback
    // instead of forcing a private UI face over arbitrary prompt content.
    var font = span.code
        ? $.NSFont.userFixedPitchFontOfSize(size)
        : $.NSFont.userFontOfSize(size);
    if (!font) {
        font = $.NSFont.systemFontOfSize(size);
    }

    var traits = 0;
    if (span.bold || span.heading) {
        traits |= Number($.NSBoldFontMask);
    }
    if (span.italic) {
        traits |= Number($.NSItalicFontMask);
    }
    if (traits !== 0) {
        var converted = $.NSFontManager.sharedFontManager.convertFontToHaveTrait(
            font,
            traits
        );
        if (converted) {
            font = converted;
        }
    }
    return font;
}

function detectCenteredAlignmentValue() {
    if (centeredAlignmentValue !== null) {
        return centeredAlignmentValue;
    }

    // JXA has exposed conflicting numeric NSTextAlignment constants across
    // Intel and Apple Silicon. Select the candidate that actually lays out centered.
    var width = 200;
    var text = "Center probe\n";
    var candidates = [
        Number($.NSTextAlignmentCenter),
        Number($.NSTextAlignmentRight)
    ];
    var bestDistance = Number.POSITIVE_INFINITY;

    candidates.forEach(function (candidate) {
        var probe = makeMutableAttributedString(text);
        var style = $.NSMutableParagraphStyle.alloc.init;
        style.setAlignment(candidate);
        probe.addAttributeValueRange(
            $.NSParagraphStyleAttributeName,
            style,
            $.NSMakeRange(0, String(text).length)
        );

        var view = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, width, 60));
        view.textContainer.setContainerSize($.NSMakeSize(width, 1000));
        view.textContainer.setWidthTracksTextView(true);
        view.textStorage.setAttributedString(probe);
        view.layoutManager.ensureLayoutForTextContainer(view.textContainer);
        var used = view.layoutManager.lineFragmentUsedRectForGlyphAtIndexEffectiveRange(
            0,
            null
        );
        var actualX = Number(ObjC.unwrap(used.origin.x));
        var expectedX = (width - Number(ObjC.unwrap(used.size.width))) / 2;
        var distance = Math.abs(actualX - expectedX);
        if (distance < bestDistance) {
            bestDistance = distance;
            centeredAlignmentValue = candidate;
        }
    });

    return centeredAlignmentValue;
}

function paragraphStyleForBlock(block) {
    var paragraph = block.paragraph || {};
    var style = $.NSMutableParagraphStyle.alloc.init;
    var indent = Number(paragraph.indent || 0);
    var containerIndent = Number(paragraph.container_indent || 0);
    style.setFirstLineHeadIndent(
        paragraph.first_indent === undefined
            ? indent
            : Number(paragraph.first_indent)
    );
    style.setHeadIndent(indent);
    style.setParagraphSpacing(Number(paragraph.spacing || 0));
    style.setParagraphSpacingBefore(Number(paragraph.spacing_before || 0));
    style.setLineSpacing(Number(paragraph.line_spacing || 0));
    if (paragraph.alignment === "center") {
        style.setAlignment(detectCenteredAlignmentValue());
    }
    if (block.type === "code_block") {
        if (containerIndent > 0) {
            style.setFirstLineHeadIndent(indent - containerIndent);
            style.setHeadIndent(indent - containerIndent);
        }
        // JXA does not reliably bridge a JavaScript array for setTextBlocks:.
        var textBlocks = $.NSMutableArray.array;
        if (containerIndent > 0) {
            // The transparent outer block supplies list/quote indentation while
            // the inner 100%-wide block paints only the code paragraph.
            var containerBlock = $.NSTextBlock.alloc.init;
            containerBlock.setContentWidthType(
                CODE_BLOCK_CONTENT_WIDTH_PERCENT,
                $.NSTextBlockPercentageValueType
            );
            containerBlock.setWidthTypeForLayerEdge(
                containerIndent,
                $.NSTextBlockAbsoluteValueType,
                $.NSTextBlockPadding,
                $.NSMinXEdge
            );
            textBlocks.addObject(containerBlock);
        }
        var textBlock = $.NSTextBlock.alloc.init;
        textBlock.setBackgroundColor($.NSColor.controlBackgroundColor);
        textBlock.setContentWidthType(
            CODE_BLOCK_CONTENT_WIDTH_PERCENT,
            $.NSTextBlockPercentageValueType
        );
        textBlock.setWidthTypeForLayer(
            CODE_BLOCK_PADDING,
            $.NSTextBlockAbsoluteValueType,
            $.NSTextBlockPadding
        );
        textBlocks.addObject(textBlock);
        style.setTextBlocks(textBlocks);
    }
    return style;
}

function blockSeparator(block, hasNextBlock) {
    if (!hasNextBlock) {
        return "";
    }
    return (block.paragraph_break ? "\n" : "") +
        (block.blank_line_after ? "\n" : "");
}

function makeAttributedBlocks(blocks, fallbackText) {
    if (!promptBlocksAreValid(blocks)) {
        return makeFallbackAttributedString(fallbackText);
    }

    var fullText = "";
    blocks.forEach(function (block, index) {
        block.spans.forEach(function (span) {
            fullText += span.text;
        });
        fullText += blockSeparator(block, index + 1 < blocks.length);
    });

    var value = makeMutableAttributedString(fullText);
    var baseSize = Number(ObjC.unwrap($.NSFont.systemFontSize));
    var documentFont = $.NSFont.userFontOfSize(baseSize);
    if (!documentFont) {
        documentFont = $.NSFont.systemFontOfSize(baseSize);
    }
    if (fullText.length > 0) {
        var fullRange = $.NSMakeRange(0, fullText.length);
        value.addAttributeValueRange($.NSFontAttributeName, documentFont, fullRange);
        value.addAttributeValueRange(
            $.NSForegroundColorAttributeName,
            $.NSColor.labelColor,
            fullRange
        );
    }

    // JavaScript string lengths are UTF-16 code units, matching Cocoa NSRange.
    var blockOffset = 0;
    blocks.forEach(function (block, blockIndex) {
        var contentLength = 0;
        block.spans.forEach(function (span) {
            contentLength += span.text.length;
        });
        var separator = blockSeparator(block, blockIndex + 1 < blocks.length);
        var paragraphBreakLength =
            block.paragraph_break && blockIndex + 1 < blocks.length ? 1 : 0;
        // The first separator newline terminates this TextKit paragraph and must
        // share its style. A second newline is only neutral inter-block spacing.
        var paragraphLength = contentLength + paragraphBreakLength;
        if (paragraphLength > 0) {
            value.addAttributeValueRange(
                $.NSParagraphStyleAttributeName,
                paragraphStyleForBlock(block),
                $.NSMakeRange(blockOffset, paragraphLength)
            );
        }

        var spanOffset = blockOffset;
        block.spans.forEach(function (span) {
            var length = span.text.length;
            if (length === 0) {
                return;
            }
            var range = $.NSMakeRange(spanOffset, length);
            value.addAttributeValueRange(
                $.NSFontAttributeName,
                fontForSpan(span, baseSize),
                range
            );
            value.addAttributeValueRange(
                $.NSForegroundColorAttributeName,
                span.secondary ? $.NSColor.secondaryLabelColor : $.NSColor.labelColor,
                range
            );
            if (span.background && block.type !== "code_block") {
                value.addAttributeValueRange(
                    $.NSBackgroundColorAttributeName,
                    $.NSColor.controlBackgroundColor,
                    range
                );
            }
            if (typeof span.link === "string") {
                var url = $.NSURL.URLWithString(span.link);
                if (url) {
                    value.addAttributeValueRange($.NSLinkAttributeName, url, range);
                    value.addAttributeValueRange(
                        $.NSForegroundColorAttributeName,
                        $.NSColor.linkColor,
                        range
                    );
                    value.addAttributeValueRange(
                        $.NSUnderlineStyleAttributeName,
                        $(Number($.NSUnderlineStyleSingle)),
                        range
                    );
                }
            }
            spanOffset += length;
        });

        blockOffset += contentLength + separator.length;
    });
    return value;
}

function measurePromptContentHeight(promptValue) {
    // Measuring in a throwaway view avoids disturbing selection and scroll state
    // in the prompt view when the rendered/plain toggle is used.
    var measurementTextView = makeTextView(
        TEXT_AREA_WIDTH,
        MIN_PROMPT_AREA_HEIGHT,
        false
    );
    measurementTextView.textStorage.setAttributedString(promptValue);
    var layoutManager = measurementTextView.layoutManager;
    var textContainer = measurementTextView.textContainer;
    var glyphRange = layoutManager.glyphRangeForTextContainer(textContainer);
    layoutManager.ensureLayoutForGlyphRange(glyphRange);
    var usedRect = layoutManager.usedRectForTextContainer(textContainer);
    var insetHeight = Number(
        ObjC.unwrap(measurementTextView.textContainerInset.height)
    );
    return Math.ceil(Number(ObjC.unwrap(usedRect.size.height)) + insetHeight * 2);
}

function resolvePromptViewportHeight(contentHeights) {
    var visibleScreenHeight = 800;
    var mainScreen = $.NSScreen.mainScreen;
    if (mainScreen) {
        visibleScreenHeight = Number(ObjC.unwrap(mainScreen.visibleFrame.size.height));
    }

    var screenBound = Math.floor(visibleScreenHeight * PROMPT_SCREEN_HEIGHT_RATIO);
    var maximumHeight = Math.max(
        MIN_PROMPT_AREA_HEIGHT,
        Math.min(MAX_PROMPT_AREA_HEIGHT, screenBound)
    );
    var desiredHeight = Math.max.apply(null, contentHeights);
    return Math.max(MIN_PROMPT_AREA_HEIGHT, Math.min(maximumHeight, desiredHeight));
}

function applyPromptValue(promptValue, contentHeight) {
    promptTextView.setFrameSize(
        $.NSMakeSize(TEXT_AREA_WIDTH, Math.max(promptViewportHeight, contentHeight))
    );
    promptTextView.textStorage.setAttributedString(promptValue);
    var layoutManager = promptTextView.layoutManager;
    var textContainer = promptTextView.textContainer;
    var glyphRange = layoutManager.glyphRangeForTextContainer(textContainer);
    layoutManager.ensureLayoutForGlyphRange(glyphRange);
    promptTextView.setNeedsDisplay(true);
    if (promptScrollView) {
        promptScrollView.contentView.setNeedsDisplay(true);
        promptScrollView.reflectScrolledClipView(promptScrollView.contentView);
    }
}

function makeTextView(width, height, editable) {
    var textView = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, width, height));
    textView.setRichText(!editable);
    textView.setEditable(editable);
    textView.setSelectable(true);
    textView.setFont(
        $.NSFont.systemFontOfSize(Number(ObjC.unwrap($.NSFont.systemFontSize)))
    );
    textView.setHorizontallyResizable(false);
    textView.setVerticallyResizable(true);
    textView.setAutoresizingMask($.NSViewWidthSizable);
    textView.setTextContainerInset(
        $.NSMakeSize(8, editable ? 8 : PROMPT_TEXT_INSET_Y)
    );
    textView.textContainer.setContainerSize($.NSMakeSize(width, 10000000));
    textView.textContainer.setWidthTracksTextView(true);
    return textView;
}

function makeScrollView(frame, textView, transparent) {
    var scrollView = $.NSScrollView.alloc.initWithFrame(frame);
    scrollView.setHasVerticalScroller(true);
    scrollView.setAutohidesScrollers(true);
    if (transparent) {
        textView.setDrawsBackground(false);
        scrollView.setDrawsBackground(false);
        scrollView.contentView.setDrawsBackground(false);
        scrollView.setBorderType($.NSNoBorder);
    } else {
        scrollView.setBorderType($.NSBezelBorder);
    }
    scrollView.setDocumentView(textView);
    return scrollView;
}

function run(argv) {
    var title = argv[0];
    var iconPath = argv[1];
    var parsedTimeout = Number(argv[2]);
    var timeoutSeconds = isFinite(parsedTimeout) && parsedTimeout >= 0
        ? parsedTimeout
        : 3600;
    var promptDocument = readPromptDocument();
    var fallbackText = promptDocument.fallback_text;
    try {
        renderedPrompt = makeAttributedBlocks(promptDocument.rendered_blocks, fallbackText);
        plainPrompt = makeAttributedBlocks(promptDocument.plain_blocks, fallbackText);
    } catch (_error) {
        renderedPrompt = makeFallbackAttributedString(fallbackText);
        plainPrompt = renderedPrompt;
        promptDocument.show_format_toggle = false;
    }
    var showFormatToggle = Boolean(promptDocument.show_format_toggle);

    var application = $.NSApplication.sharedApplication;
    application.setActivationPolicy($.NSApplicationActivationPolicyRegular);
    $.NSProcessInfo.processInfo.setProcessName(title);
    application.performSelector($.NSSelectorFromString("finishLaunching"));
    var icon = $.NSImage.alloc.initWithContentsOfFile(iconPath);
    if (icon) {
        application.setApplicationIconImage(icon);
    }

    var alert = $.NSAlert.alloc.init;
    if (icon) {
        alert.setIcon(icon);
    }
    alert.setMessageText(title);
    var okButton = alert.addButtonWithTitle("OK");
    okButton.setKeyEquivalent("\r");
    okButton.setKeyEquivalentModifierMask($.NSEventModifierFlagCommand);
    var cancelButton = alert.addButtonWithTitle("Cancel");
    cancelButton.setKeyEquivalent("\u001b");
    cancelButton.setKeyEquivalentModifierMask(0);

    renderedPromptContentHeight = measurePromptContentHeight(renderedPrompt);
    plainPromptContentHeight = showFormatToggle
        ? measurePromptContentHeight(plainPrompt)
        : renderedPromptContentHeight;
    promptViewportHeight = resolvePromptViewportHeight(
        [renderedPromptContentHeight, plainPromptContentHeight]
    );
    promptTextView = makeTextView(
        TEXT_AREA_WIDTH,
        Math.max(promptViewportHeight, renderedPromptContentHeight),
        false
    );
    promptTextView.textStorage.setAttributedString(renderedPrompt);
    var promptPaneGap = showFormatToggle ? TOGGLE_ROW_HEIGHT : PANE_GAP;
    promptScrollView = makeScrollView(
        $.NSMakeRect(
            0,
            RESPONSE_AREA_HEIGHT + promptPaneGap,
            TEXT_AREA_WIDTH,
            promptViewportHeight
        ),
        promptTextView,
        true
    );

    responseTextView = makeTextView(TEXT_AREA_WIDTH, RESPONSE_AREA_HEIGHT, true);
    responseTextView.setAllowsUndo(true);
    responseTextView.setAutomaticQuoteSubstitutionEnabled(false);
    responseTextView.setAutomaticDashSubstitutionEnabled(false);
    var responseScrollView = makeScrollView(
        $.NSMakeRect(0, 0, TEXT_AREA_WIDTH, RESPONSE_AREA_HEIGHT),
        responseTextView,
        false
    );

    var accessoryView = $.NSView.alloc.initWithFrame(
        $.NSMakeRect(
            0,
            0,
            TEXT_AREA_WIDTH,
            promptViewportHeight + promptPaneGap + RESPONSE_AREA_HEIGHT
        )
    );
    accessoryView.addSubview(promptScrollView);

    var controller = $.AskHumanDialogController.alloc.init;
    promptTextView.setDelegate(controller);
    if (showFormatToggle) {
        toggleButton = $.NSButton.alloc.initWithFrame(
            $.NSMakeRect(
                TEXT_AREA_WIDTH - TOGGLE_BUTTON_WIDTH,
                RESPONSE_AREA_HEIGHT + 8,
                TOGGLE_BUTTON_WIDTH,
                TOGGLE_BUTTON_HEIGHT
            )
        );
        toggleButton.setTitle("Show Plain");
        toggleButton.setBezelStyle($.NSBezelStyleRounded);
        toggleButton.setTarget(controller);
        toggleButton.setAction($.NSSelectorFromString("togglePromptFormat:"));
        accessoryView.addSubview(toggleButton);
    }
    accessoryView.addSubview(responseScrollView);
    alert.setAccessoryView(accessoryView);

    installMainMenu(application, title);

    promptApplication = application;
    promptWindow = alert.window;
    promptWindow.setLevel($.NSFloatingWindowLevel);
    if (application.respondsToSelector($.NSSelectorFromString("activate"))) {
        application.performSelector($.NSSelectorFromString("activate"));
    } else {
        application.activateIgnoringOtherApps(true);
    }
    promptWindow.makeKeyAndOrderFront(null);
    promptWindow.performSelector($.NSSelectorFromString("orderFrontRegardless"));
    promptWindow.setInitialFirstResponder(responseTextView);

    var lowerTimer = $.NSTimer.timerWithTimeIntervalTargetSelectorUserInfoRepeats(
        WINDOW_LEVEL_GRACE_SECONDS,
        controller,
        $.NSSelectorFromString("lowerWindow:"),
        null,
        false
    );
    var timeoutTimer = $.NSTimer.timerWithTimeIntervalTargetSelectorUserInfoRepeats(
        timeoutSeconds,
        controller,
        $.NSSelectorFromString("timeOut:"),
        null,
        false
    );
    $.NSRunLoop.currentRunLoop.addTimerForMode(lowerTimer, $.NSModalPanelRunLoopMode);
    $.NSRunLoop.currentRunLoop.addTimerForMode(timeoutTimer, $.NSModalPanelRunLoopMode);

    var result = Number(ObjC.unwrap(alert.runModal));
    lowerTimer.performSelector($.NSSelectorFromString("invalidate"));
    timeoutTimer.performSelector($.NSSelectorFromString("invalidate"));
    if (attentionRequest !== 0) {
        application.cancelUserAttentionRequest(attentionRequest);
    }
    application.setActivationPolicy($.NSApplicationActivationPolicyProhibited);

    if (timedOut || result === TIMEOUT_MODAL_RESPONSE) {
        return JSON.stringify({status: "timeout"});
    }
    if (result === Number($.NSAlertFirstButtonReturn)) {
        return JSON.stringify({status: "ok", value: ObjC.unwrap(responseTextView.string)});
    }
    return JSON.stringify({status: "cancelled"});
}
