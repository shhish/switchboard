// Construct explicit range test cases without deployment-specific literals.
func ip(_ octets: any CustomStringConvertible...) -> String {
  octets.map { $0.description }.joined(separator: ".")
}
