from rvs import config as cfg_mod


def _domain_endpoints(domain: str) -> cfg_mod.NativeRegistryEndpoints:
    def package(kind: str) -> cfg_mod.PackageRegistryEndpoints:
        return cfg_mod.PackageRegistryEndpoints(
            read_base_url=f"https://{kind}.{domain}",
            push_base_url=f"https://push.{kind}.{domain}",
            mirror_base_url=f"https://mirror.{kind}.{domain}",
        )

    return cfg_mod.NativeRegistryEndpoints(
        pypi=package("pypi"),
        npm=package("npm"),
        maven=package("maven"),
        oci_registry_base_url=f"https://oci.{domain}",
    )


def test_repository_domain_summary_recognizes_service_domain_family() -> None:
    endpoints = _domain_endpoints("packages.enterprise.example")

    assert cfg_mod.repository_domain_summary(endpoints) == "packages.enterprise.example"


def test_repository_domain_summary_recognizes_localhost_routes() -> None:
    endpoints = cfg_mod.NativeRegistryEndpoints(
        pypi=cfg_mod.PackageRegistryEndpoints(
            read_base_url="http://localhost:8081/pypi",
            push_base_url="http://localhost:8082/pypi",
            mirror_base_url="http://localhost:8083/pypi",
        ),
        npm=cfg_mod.PackageRegistryEndpoints(
            read_base_url="http://localhost:8081/npm",
            push_base_url="http://localhost:8082/npm",
            mirror_base_url="http://localhost:8083/npm",
        ),
        maven=cfg_mod.PackageRegistryEndpoints(
            read_base_url="http://localhost:8081/maven",
            push_base_url="http://localhost:8082/maven",
            mirror_base_url="http://localhost:8083/maven",
        ),
        oci_registry_base_url="http://localhost:8084/oci",
    )

    assert cfg_mod.repository_domain_summary(endpoints) == "localhost"


def test_repository_domain_summary_marks_unrelated_hosts_as_custom() -> None:
    endpoints = _domain_endpoints("packages.enterprise.example")
    custom = cfg_mod.NativeRegistryEndpoints(
        pypi=endpoints.pypi,
        npm=endpoints.npm,
        maven=endpoints.maven,
        oci_registry_base_url="https://registry.example.test",
    )

    assert cfg_mod.repository_domain_summary(custom) == "custom endpoints"
