ObjC.import("AppKit");

var TIMEOUT_MODAL_RESPONSE = 4242;
var WINDOW_LEVEL_GRACE_SECONDS = 0.5;
var TEXT_AREA_WIDTH = 640;
var MIN_PROMPT_AREA_HEIGHT = 150;
var MAX_PROMPT_AREA_HEIGHT = 520;
var PROMPT_SCREEN_HEIGHT_RATIO = 0.45;
var RESPONSE_AREA_HEIGHT = 180;
var PANE_GAP = 16;

var promptApplication = null;
var promptWindow = null;
var timedOut = false;
var attentionRequest = 0;

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
        }
    }
});

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

function resolvePromptAreaHeight(promptTextView) {
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
    var layoutManager = promptTextView.layoutManager;
    var textContainer = promptTextView.textContainer;
    var glyphRange = layoutManager.glyphRangeForTextContainer(textContainer);
    layoutManager.ensureLayoutForGlyphRange(glyphRange);
    var usedRect = layoutManager.usedRectForTextContainer(textContainer);
    var usedHeight = Number(ObjC.unwrap(usedRect.size.height));
    var insetHeight = Number(ObjC.unwrap(promptTextView.textContainerInset.height));
    var desiredHeight = Math.ceil(usedHeight + insetHeight * 2);
    return Math.max(MIN_PROMPT_AREA_HEIGHT, Math.min(maximumHeight, desiredHeight));
}

function makeTextView(width, height, editable) {
    var textView = $.NSTextView.alloc.initWithFrame($.NSMakeRect(0, 0, width, height));
    textView.setRichText(false);
    textView.setEditable(editable);
    textView.setSelectable(true);
    textView.setFont(
        $.NSFont.systemFontOfSize(Number(ObjC.unwrap($.NSFont.systemFontSize)))
    );
    textView.setHorizontallyResizable(false);
    textView.setVerticallyResizable(true);
    textView.setAutoresizingMask($.NSViewWidthSizable);
    textView.setTextContainerInset($.NSMakeSize(8, 8));
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
    var prompt = argv[1];
    var iconPath = argv[2];
    var parsedTimeout = Number(argv[3]);
    var timeoutSeconds = isFinite(parsedTimeout) && parsedTimeout >= 0
        ? parsedTimeout
        : 3600;

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

    var promptTextView = makeTextView(TEXT_AREA_WIDTH, MIN_PROMPT_AREA_HEIGHT, false);
    promptTextView.setString(prompt);
    var promptAreaHeight = resolvePromptAreaHeight(promptTextView);
    promptTextView.setFrameSize($.NSMakeSize(TEXT_AREA_WIDTH, promptAreaHeight));
    var promptScrollView = makeScrollView(
        $.NSMakeRect(
            0,
            RESPONSE_AREA_HEIGHT + PANE_GAP,
            TEXT_AREA_WIDTH,
            promptAreaHeight
        ),
        promptTextView,
        true
    );

    var responseTextView = makeTextView(TEXT_AREA_WIDTH, RESPONSE_AREA_HEIGHT, true);
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
            promptAreaHeight + PANE_GAP + RESPONSE_AREA_HEIGHT
        )
    );
    accessoryView.addSubview(promptScrollView);
    accessoryView.addSubview(responseScrollView);
    alert.setAccessoryView(accessoryView);

    var controller = $.AskHumanDialogController.alloc.init;
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
