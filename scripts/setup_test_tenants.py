"""Setup test tenants for Ruperto development."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.models import StoreVertical
from ruperto.repository import BusinessRepository
from ruperto.schemas import MunicipalAreaCreateRequest, MunicipalCategoryCreateRequest


async def setup_test_tenants() -> None:
    """Create test tenants for development and testing."""
    settings = Settings()

    with TemporaryDirectory(prefix="ruperto-tenants-") as tmp_dir:
        db_path = Path(tmp_dir) / "tenants.db"
        tenant_settings = settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{db_path}"})
        runtime = create_database_runtime(tenant_settings)
        await init_database(settings=tenant_settings, runtime=runtime)

        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)

            # Create "Mi Roti" tenant (ordering vertical)
            roti_store = await repository.create_store_profile(
                store_name="Mi Roti",
                bot_name="Ruperto Mi Roti",
                store_description="Tenant de pruebas para pedidos.",
                assistant_personality="Claro, ágil y amable.",
                vertical=StoreVertical.ORDERING,
                slug="mi-roti",
            )

            # Create "Mi Muni" tenant (municipal vertical)
            muni_store = await repository.create_store_profile(
                store_name="Mi Muni",
                bot_name="Ruperto Mi Muni",
                store_description="Tenant de pruebas para atención municipal.",
                assistant_personality="Claro, cercano y resolutivo.",
                vertical=StoreVertical.MUNICIPAL,
                slug="mi-muni",
            )

            print("✅ Created test tenants:")
            print(f"   - Mi Roti (ID: {roti_store.id}, Vertical: {roti_store.vertical.value})")
            print(f"   - Mi Muni (ID: {muni_store.id}, Vertical: {muni_store.vertical.value})")
            print(f"   - Database: {db_path}")

            # Create some test data for municipal tenant
            if muni_store.vertical == StoreVertical.MUNICIPAL:
                # Add municipal areas
                area1 = await repository.create_municipal_area(
                    store_id=muni_store.id,
                    payload=MunicipalAreaCreateRequest(
                        name="Alumbrado público",
                        description="Problemas con iluminación urbana",
                    ),
                )
                area2 = await repository.create_municipal_area(
                    store_id=muni_store.id,
                    payload=MunicipalAreaCreateRequest(
                        name="Limpieza urbana",
                        description="Limpieza de calles y espacios públicos",
                    ),
                )

                # Add municipal categories
                cat1 = await repository.create_municipal_category(
                    area_id=area1.id,
                    payload=MunicipalCategoryCreateRequest(
                        name="Lámpara apagada",
                        requires_precise_location=True,
                    ),
                )
                cat2 = await repository.create_municipal_category(
                    area_id=area1.id,
                    payload=MunicipalCategoryCreateRequest(
                        name="Poste caído",
                        requires_precise_location=True,
                    ),
                )
                cat3 = await repository.create_municipal_category(
                    area_id=area2.id,
                    payload=MunicipalCategoryCreateRequest(
                        name="Basura acumulada",
                        requires_precise_location=False,
                    ),
                )

                print("✅ Added municipal catalog for Mi Muni:")
                print(f"   - Areas: {area1.name}, {area2.name}")
                print(f"   - Categories: {cat1.name}, {cat2.name}, {cat3.name}")

            await session.commit()

        await runtime.engine.dispose()


if __name__ == "__main__":
    print("🚀 Setting up test tenants...")
    asyncio.run(setup_test_tenants())
    print("🎉 Test tenants setup complete!")
