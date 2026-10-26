import AppKit
import ApplicationServices
import CryptoKit
import Foundation

// This helper never launches apps, uses the clipboard, presses keys or invokes actions.
struct Request: Decodable {
    let action: String
    let chat_label: String
    let fingerprint: String?
    let text: String?
}
struct Probe {
    let editor: AXUIElement
    let fingerprint: String
}
enum Stop: Error { case reason(String) }
func read(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}
func element(_ value: CFTypeRef?) -> AXUIElement? {
    guard let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}
func probe(_ label: String) throws -> Probe {
    guard AXIsProcessTrusted() else { throw Stop.reason("accessibility_permission_required") }
    guard !label.isEmpty, let front = NSWorkspace.shared.frontmostApplication,
          front.bundleIdentifier == "com.tencent.xinWeChat" else { throw Stop.reason("wechat_must_be_foreground") }
    guard front.bundleURL?.resolvingSymlinksInPath().path == "/Applications/WeChat.app" else {
        throw Stop.reason("official_wechat_required")
    }
    let app = AXUIElementCreateApplication(front.processIdentifier)
    AXUIElementSetMessagingTimeout(app, 2)
    guard let window = element(read(app, kAXFocusedWindowAttribute)),
          let editor = element(read(app, kAXFocusedUIElementAttribute)) else {
        throw Stop.reason("accessible_composer_unavailable")
    }
    guard read(editor, kAXRoleAttribute) as? String == kAXTextAreaRole,
          let value = read(editor, kAXValueAttribute) as? String, value.isEmpty else {
        throw Stop.reason("focus_empty_message_composer")
    }
    var settable = DarwinBoolean(false)
    guard AXUIElementIsAttributeSettable(editor, kAXValueAttribute as CFString, &settable) == .success,
          settable.boolValue else { throw Stop.reason("composer_not_writable") }
    var rows: [[String]] = []
    var stack: [(AXUIElement, String)] = [(window, "0")]
    var foundLabel = false
    var foundEditor = false
    while let (node, path) = stack.popLast() {
        guard rows.count < 1500 else { throw Stop.reason("context_truncated") }
        let role = read(node, kAXRoleAttribute) as? String ?? ""
        let title = read(node, kAXTitleAttribute) as? String ?? ""
        let description = read(node, kAXDescriptionAttribute) as? String ?? ""
        let value = read(node, kAXValueAttribute) as? String ?? ""
        // Exact visible label is only a guard; account/chat identity is user-confirmed.
        if (role == kAXStaticTextRole || role == kAXWindowRole), [title, value].contains(label) {
            foundLabel = true
        }
        if CFEqual(node, editor) { foundEditor = true }
        rows.append([path, role, title, description, value])
        if let children = read(node, kAXChildrenAttribute) as? [AXUIElement] {
            for (index, child) in children.enumerated().reversed() { stack.append((child, "\(path).\(index)")) }
        }
    }
    guard foundLabel, foundEditor, rows.count > 4 else { throw Stop.reason("conversation_context_unverifiable") }
    rows.append(["pid", String(front.processIdentifier)])
    let data = try JSONSerialization.data(withJSONObject: rows)
    let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    return Probe(editor: editor, fingerprint: digest)
}
func output(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) { print(text) }
}
do {
    let request = try JSONDecoder().decode(Request.self, from: FileHandle.standardInput.readDataToEndOfFile())
    guard ["probe", "stage"].contains(request.action) else { throw Stop.reason("unsupported_action") }
    let before = try probe(request.chat_label)
    if request.action == "probe" {
        output(["status": "ready", "fingerprint": before.fingerprint, "identity_verification": "manual_current_chat"])
    } else {
        guard let text = request.text, !text.isEmpty, text.utf8.count <= 32000,
              request.fingerprint == before.fingerprint else { throw Stop.reason("stale_or_invalid_draft") }
        let current = try probe(request.chat_label)
        guard current.fingerprint == before.fingerprint, CFEqual(current.editor, before.editor) else {
            throw Stop.reason("context_changed")
        }
        let error = AXUIElementSetAttributeValue(current.editor, kAXValueAttribute as CFString, text as CFString)
        let matches = read(current.editor, kAXValueAttribute) as? String == text
        output(["status": error == .success && matches ? "staged" : "unknown", "sent": false])
    }
} catch Stop.reason(let code) {
    output(["status": "blocked", "reason": code, "sent": false])
} catch {
    output(["status": "blocked", "reason": "native_adapter_failed", "sent": false])
}
