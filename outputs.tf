output "api_url" {
  description = "Служебный URL API-шлюза."
  value       = "https://${yandex_api_gateway.archives.domain}"
}
