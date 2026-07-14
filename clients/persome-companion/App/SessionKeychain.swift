#if canImport(Security)
import Foundation
import PersomeCompanionCore
import Security

struct SessionKeychain {
    private let service = "app.persome.companion.bridge"
    private let account = "paired-session"

    func save(_ session: CompanionSession) throws {
        let data = try JSONEncoder().encode(session)
        try delete()
        let status = SecItemAdd(
            [
                kSecClass: kSecClassGenericPassword,
                kSecAttrService: service,
                kSecAttrAccount: account,
                kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                kSecValueData: data,
            ] as CFDictionary,
            nil
        )
        guard status == errSecSuccess else { throw KeychainError(status) }
    }

    func load() throws -> CompanionSession? {
        var result: CFTypeRef?
        let status = SecItemCopyMatching(
            [
                kSecClass: kSecClassGenericPassword,
                kSecAttrService: service,
                kSecAttrAccount: account,
                kSecReturnData: true,
                kSecMatchLimit: kSecMatchLimitOne,
            ] as CFDictionary,
            &result
        )
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else {
            throw KeychainError(status)
        }
        return try JSONDecoder().decode(CompanionSession.self, from: data)
    }

    func delete() throws {
        let status = SecItemDelete(
            [
                kSecClass: kSecClassGenericPassword,
                kSecAttrService: service,
                kSecAttrAccount: account,
            ] as CFDictionary
        )
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError(status)
        }
    }
}

struct KeychainError: Error {
    let status: OSStatus
    init(_ status: OSStatus) { self.status = status }
}
#endif
