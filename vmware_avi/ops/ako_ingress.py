"""AKO Ingress annotation validation and diagnostics."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from vmware_avi._safety import sanitize
from vmware_avi.config import load_config
from vmware_avi.k8s_connection import K8sConnectionManager

console = Console()


def check_ingress_annotations(namespace: str, context: str | None = None) -> None:
    """Validate Ingress annotations in a namespace."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)

    from kubernetes.client import NetworkingV1Api

    net_v1 = NetworkingV1Api(k8s.get_client(context))

    ingresses = net_v1.list_namespaced_ingress(namespace)

    # Fetch every secret in the namespace ONCE, then check TLS references by
    # local set membership — the previous code issued a read_namespaced_secret
    # per Ingress per TLS entry (nested N+1). ``None`` means the list failed, so
    # we skip (rather than falsely report) TLS existence checks.
    secret_names: set[str] | None = set()
    try:
        secrets = k8s.core_v1(context).list_namespaced_secret(namespace)
        secret_names = {s.metadata.name for s in secrets.items if s.metadata and s.metadata.name}
    except Exception as exc:
        console.print(
            f"[yellow]Could not list secrets in '{namespace}': "
            f"{sanitize(str(exc)[:100])} — TLS secret existence not verified.[/yellow]"
        )
        secret_names = None

    table = Table(title=f"Ingress Annotations Check: {namespace}")
    table.add_column("Ingress")
    table.add_column("IngressClass")
    table.add_column("Issues")
    table.add_column("Status")

    for ing in ingresses.items:
        name = ing.metadata.name
        annotations = ing.metadata.annotations or {}
        ingress_class = ing.spec.ingress_class_name or annotations.get(
            "kubernetes.io/ingress.class", ""
        )

        issues: list[str] = []

        if not ingress_class:
            issues.append("No IngressClass specified")
        elif ingress_class not in ("avi", "avi-lb"):
            issues.append(f"IngressClass '{ingress_class}' may not be AKO")

        if ing.spec.tls and secret_names is not None:
            for tls in ing.spec.tls:
                if tls.secret_name and tls.secret_name not in secret_names:
                    issues.append(f"TLS secret '{tls.secret_name}' not found")

        status = "[green]OK[/green]" if not issues else "[red]ISSUES[/red]"
        table.add_row(name, ingress_class or "N/A", "; ".join(issues) or "-", status)

    console.print(table)


def show_ingress_map(context: str | None = None) -> None:
    """Show Ingress to VS mapping across all namespaces."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)

    from kubernetes.client import NetworkingV1Api

    net_v1 = NetworkingV1Api(k8s.get_client(context))
    ingresses = net_v1.list_ingress_for_all_namespaces()

    table = Table(title="Ingress → VS Mapping")
    table.add_column("Namespace")
    table.add_column("Ingress")
    table.add_column("Host")
    table.add_column("IngressClass")

    for ing in ingresses.items:
        ns = ing.metadata.namespace
        name = ing.metadata.name
        annotations = ing.metadata.annotations or {}
        ingress_class = ing.spec.ingress_class_name or annotations.get(
            "kubernetes.io/ingress.class", ""
        )
        hosts = []
        if ing.spec.rules:
            hosts = [r.host or "*" for r in ing.spec.rules]

        table.add_row(ns, name, ", ".join(hosts), ingress_class or "N/A")

    console.print(table)


def _status(exc: BaseException) -> int | None:
    """The HTTP status of a kubernetes ``ApiException``, else None."""
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _describe(exc: BaseException) -> str:
    """One short, markup-safe line for an error reading a Kubernetes object."""
    from rich.markup import escape

    status = _status(exc)
    reason = getattr(exc, "reason", None) or type(exc).__name__
    return escape(f"HTTP {status} {reason}" if status else str(reason))[:120]


def diagnose_ingress(name: str, namespace: str = "default", context: str | None = None) -> None:
    """Deep diagnosis of a specific Ingress."""
    cfg = load_config()
    k8s = K8sConnectionManager.from_config(cfg)

    from kubernetes.client import NetworkingV1Api

    net_v1 = NetworkingV1Api(k8s.get_client(context))

    try:
        ing = net_v1.read_namespaced_ingress(name, namespace)
    except Exception as exc:
        if _status(exc) == 404:
            console.print(
                f"[red]Ingress '{name}' not found in namespace '{namespace}'. Run "
                "list_ako_ingresses to list the Ingresses AKO manages, or check the "
                f"namespace with: kubectl get ingress -n {namespace}[/red]"
            )
        else:
            # A 403 or a dropped connection says nothing about whether it exists.
            console.print(
                f"[red]Could not read Ingress '{name}' in namespace '{namespace}' "
                f"({_describe(exc)}). Check the kube-context and its RBAC: kubectl get "
                f"ingress {name} -n {namespace}[/red]",
                markup=True,
            )
        raise SystemExit(1)

    console.print(f"\n[bold]Diagnosing Ingress: {namespace}/{name}[/bold]\n")

    annotations = ing.metadata.annotations or {}
    console.print("[bold]Annotations:[/bold]")
    for k, v in annotations.items():
        console.print(f"  {sanitize(k)}: {sanitize(v)}")

    issues: list[str] = []
    suggestions: list[str] = []

    # Check IngressClass
    ingress_class = ing.spec.ingress_class_name or annotations.get(
        "kubernetes.io/ingress.class", ""
    )
    if not ingress_class:
        issues.append("No IngressClass specified")
        suggestions.append(
            "Add spec.ingressClassName: 'avi-lb' or annotation kubernetes.io/ingress.class: 'avi'"
        )
    elif ingress_class not in ("avi", "avi-lb"):
        issues.append(f"IngressClass '{ingress_class}' is not AKO")
        suggestions.append("Change to 'avi' or 'avi-lb'")

    # Check TLS secrets
    if ing.spec.tls:
        for tls in ing.spec.tls:
            if tls.secret_name:
                try:
                    k8s.core_v1(context).read_namespaced_secret(tls.secret_name, namespace)
                except Exception as exc:
                    if _status(exc) == 404:
                        issues.append(f"TLS secret '{tls.secret_name}' missing")
                        suggestions.append(
                            f"Create secret: kubectl create secret tls {tls.secret_name} ..."
                        )
                    else:
                        issues.append(
                            f"TLS secret '{tls.secret_name}' could not be read ({_describe(exc)})"
                        )

    # Check backend services exist
    if ing.spec.rules:
        for rule in ing.spec.rules:
            if rule.http and rule.http.paths:
                for path in rule.http.paths:
                    if (
                        not path.backend
                        or not path.backend.service
                        or not path.backend.service.name
                    ):
                        issues.append("Path has no backend service configured")
                        continue
                    svc_name = path.backend.service.name
                    try:
                        k8s.core_v1(context).read_namespaced_service(svc_name, namespace)
                    except Exception as exc:
                        if _status(exc) == 404:
                            issues.append(f"Backend service '{svc_name}' not found")
                            suggestions.append(
                                f"Verify service '{svc_name}' exists in namespace '{namespace}'"
                            )
                        else:
                            issues.append(
                                f"Backend service '{svc_name}' could not be read "
                                f"({_describe(exc)})"
                            )
                            suggestions.append(
                                f"Check RBAC and connectivity: kubectl get service {svc_name} "
                                f"-n {namespace}"
                            )

    if issues:
        console.print(f"\n[red]Issues Found ({len(issues)}):[/red]")
        for i, issue in enumerate(issues, 1):
            console.print(f"  {i}. {issue}")
        console.print("\n[yellow]Suggestions:[/yellow]")
        for i, sug in enumerate(suggestions, 1):
            console.print(f"  {i}. {sug}")
    else:
        console.print("\n[green]No issues found with Ingress configuration.[/green]")
        console.print("If VS is still not created, check AKO logs and sync status:")
        console.print("  vmware-avi ako logs --since 5m")
        console.print("  vmware-avi ako sync status")
    console.print()
