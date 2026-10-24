import Foundation
import Security
let secret = FileHandle.standardInput.readDataToEndOfFile()
if secret.isEmpty { exit(2) }
let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "pintuanya-cli", kSecAttrAccount as String: "PINTUANYA_COOKIE"]
var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: secret] as CFDictionary)
if status == errSecItemNotFound {
    var item = query
    item[kSecValueData as String] = secret
    status = SecItemAdd(item as CFDictionary, nil)
}
if status != errSecSuccess {
    FileHandle.standardError.write(Data("Keychain OSStatus: \(status)\n".utf8))
    exit(1)
}
print("Cookie 已保存至本机钥匙串：pintuanya-cli / PINTUANYA_COOKIE")
