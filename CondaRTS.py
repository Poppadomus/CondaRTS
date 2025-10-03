from __future__ import annotations

import math
import random
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, ClassVar

import pygame as pg

from src.ai import AI
from src.barracks import Barracks
from src.camera import Camera
from src.constants import (
    CONSOLE_HEIGHT,
    GDI_COLOR,
    MAP_HEIGHT,
    MAP_WIDTH,
    NOD_COLOR,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    TILE_SIZE,
    Team,
)
from src.fog_of_war import FogOfWar
from src.game_console import GameConsole
from src.geometry import (
    calculate_formation_positions,
    is_valid_building_position,
    snap_to_grid,
)
from src.harvester import Harvester
from src.headquarters import Headquarters
from src.infantry import Infantry
from src.iron_field import IronField
from src.particle import Particle
from src.power_plant import PowerPlant
from src.projectile import Projectile
from src.shapes import draw_progress_bar
from src.tank import Tank
from src.turret import Turret
from src.war_factory import WarFactory

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from src.building import Building
    from src.game_object import GameObject


def handle_collisions(all_units: Iterable[GameObject]) -> None:
    for unit in all_units:
        for other in all_units:
            if unit != other and unit.rect.colliderect(other.rect):
                dx, dy = (
                    unit.rect.centerx - other.rect.centerx,
                    unit.rect.centery - other.rect.centery,
                )
                dist = math.sqrt(dx**2 + dy**2)
                if dist > 0:
                    push = (
                        0.3
                        if isinstance(unit, Harvester) and isinstance(other, Harvester)
                        else 0.5
                    )
                    unit.rect.x += push * dx / dist
                    unit.rect.y += push * dy / dist
                    other.rect.x -= push * dx / dist
                    other.rect.y -= push * dy / dist


def handle_attacks(
    *,
    team_units: Iterable[GameObject],
    all_units: Iterable[GameObject],
    all_buildings: Iterable[Building],
    projectiles: pg.sprite.Group[Projectile],
    particles: pg.sprite.Group[Particle],
) -> None:
    for unit in team_units:
        if isinstance(unit, (Tank, Infantry)) and unit.cooldown_timer == 0:
            closest_target, min_dist = None, float("inf")
            if unit.target_unit and unit.target_unit.health > 0:
                dist = math.sqrt(
                    (unit.rect.centerx - unit.target_unit.rect.centerx) ** 2
                    + (unit.rect.centery - unit.target_unit.rect.centery) ** 2
                )
                if dist <= unit.attack_range:
                    closest_target, min_dist = unit.target_unit, dist

            if not closest_target:
                for target in (*all_units, *all_buildings):
                    if target.team != unit.team and target.health > 0:
                        dist = math.sqrt(
                            (unit.rect.centerx - target.rect.centerx) ** 2
                            + (unit.rect.centery - target.rect.centery) ** 2
                        )
                        if dist <= unit.attack_range and dist < min_dist:
                            closest_target, min_dist = target, dist

            if closest_target:
                unit.target_unit = closest_target
                unit.target = closest_target.rect.center
                if isinstance(unit, Tank):
                    dx, dy = (
                        closest_target.rect.centerx - unit.rect.centerx,
                        closest_target.rect.centery - unit.rect.centery,
                    )
                    unit.angle = math.degrees(
                        math.atan2(dy, dx)
                    )  # Updated to match Tank's angle calculation
                    projectiles.add(
                        Projectile(
                            unit.rect.centerx,
                            unit.rect.centery,
                            closest_target,
                            unit.attack_damage,
                            unit.team,
                        )
                    )
                    unit.recoil = 5
                    barrel_angle = math.radians(unit.angle)
                    smoke_x = unit.rect.centerx + math.cos(barrel_angle) * (
                        unit.rect.width // 2 + 12
                    )
                    smoke_y = unit.rect.centery + math.sin(barrel_angle) * (
                        unit.rect.width // 2 + 12
                    )
                    for _ in range(5):
                        particles.add(
                            Particle(
                                smoke_x,
                                smoke_y,
                                random.uniform(-1.5, 1.5),
                                random.uniform(-1.5, 1.5),
                                random.randint(6, 10),
                                pg.Color(100, 100, 100),
                                20,
                            )
                        )
                else:
                    closest_target.health -= unit.attack_damage
                    closest_target.under_attack = (
                        True  # Set under_attack only when damage is applied
                    )
                    for _ in range(3):
                        particles.add(
                            Particle(
                                unit.rect.centerx,
                                unit.rect.centery,
                                random.uniform(-1, 1),
                                random.uniform(-1, 1),
                                4,
                                pg.Color(255, 200, 100),
                                10,
                            )
                        )
                    if closest_target.health <= 0:
                        closest_target.kill()
                        unit.target = unit.target_unit = None
                unit.cooldown_timer = unit.attack_cooldown


def handle_projectiles(
    *,
    projectiles: Iterable[Projectile],
    all_units: Iterable[GameObject],
    all_buildings: Iterable[Building],
) -> None:
    for projectile in projectiles:
        # Check collision with all enemy units and buildings, not just the target
        enemy_units = [
            u for u in all_units if u.team != projectile.team and u.health > 0
        ]
        enemy_buildings = [
            b for b in all_buildings if b.team != projectile.team and b.health > 0
        ]

        for e in enemy_units + enemy_buildings:
            if projectile.rect.colliderect(e.rect):
                e.health -= projectile.damage
                e.under_attack = True  # Set under_attack when damage is applied
                for _ in range(5):
                    particles.add(
                        Particle(
                            projectile.rect.centerx,
                            projectile.rect.centery,
                            random.uniform(-2, 2),
                            random.uniform(-2, 2),
                            6,
                            pg.Color(255, 200, 100),
                            15,
                        )
                    )
                projectile.kill()
                if e.health <= 0:
                    e.kill()

                break


def draw(*, surface_: pg.Surface, font_: pg.Font) -> None:
    surface_.fill(pg.Color("black"))
    surface_.blit(base_map, (-camera.rect.x, -camera.rect.y))
    for field in iron_fields:
        if field.resources > 0 and fog_of_war.is_explored(field.rect.center):
            field.draw(surface_=surface_, camera=camera, font=font_)

    for building in global_buildings:
        if building.health > 0 and (
            fog_of_war.is_visible(building.rect.center)
            or (building.is_seen and fog_of_war.is_explored(building.rect.center))
        ):
            building.draw(surface_, camera)

    fog_of_war.draw(surface_, camera)
    for unit in global_units:
        if unit.team == Team.GDI or fog_of_war.is_visible(unit.rect.center):
            if isinstance(unit, Harvester):
                unit.draw(surface=surface_, camera=camera, font=font)
            else:
                unit.draw(surface_, camera)

    for projectile in projectiles:
        if projectile.team == Team.GDI or fog_of_war.is_visible(projectile.rect.center):
            projectile.draw(surface_, camera)

    for particle in particles:
        if fog_of_war.is_visible(particle.rect.center):
            particle.draw(surface_, camera)

    interface.draw(
        surface_=surface_,
        own_buildings=[b for b in global_buildings if b.team == Team.GDI],
    )
    if selecting and select_rect:
        pg.draw.rect(surface_, (255, 255, 255), select_rect, 2)

    console.draw(surface_)


@dataclass(kw_only=True)
class ProductionInterface:
    """Interface for player."""

    WIDTH: ClassVar = 200
    MARGIN_X: ClassVar = 20
    """Margin on left and right."""
    IRON_POS_Y: ClassVar = 20
    """y position of iron value."""
    POWER_POS_Y: ClassVar = 45
    """... power value."""
    TAB_BUTTONS_POS_Y: ClassVar = 70
    """... first tab button."""
    BUY_BUTTONS_POS_Y: ClassVar = 190
    """... first buy button."""
    SELL_BUTTON_POS_Y: ClassVar = 390
    """... sell button."""
    PRODUCTION_QUEUE_POS_Y: ClassVar = 460
    """... production queue."""
    BUTTON_SPACING_Y: ClassVar = 10
    BUTTON_RADIUS: ClassVar = 5
    TAB_BUTTON_HEIGHT: ClassVar = 30
    ACTION_BUTTON_HEIGHT: ClassVar = 40
    FILL_COLOR: ClassVar = pg.Color(60, 60, 60)
    LINE_COLOR: ClassVar = pg.Color(100, 100, 100)
    ACTIVE_TAB_COLOR: ClassVar = pg.Color(0, 200, 200)
    INACTIVE_TAB_COLOR: ClassVar = pg.Color(50, 50, 50)
    ACTION_ALLOWED_COLOR: ClassVar = pg.Color(0, 200, 0)
    ACTION_BLOCKED_COLOR: ClassVar = pg.Color(200, 0, 0)
    MAX_PRODUCTION_QUEUE_LENGTH: ClassVar = 5
    PLACEMENT_VALID_COLOR = (0, 255, 0)
    PLACEMENT_INVALID_COLOR = (255, 0, 0)

    _BUTTON_WIDTH = WIDTH - 2 * MARGIN_X

    hq: Headquarters
    surface: pg.Surface = dataclass_field(init=False)
    tab_buttons: dict[str, pg.Rect] = dataclass_field(default_factory=dict)
    buy_buttons: dict[
        str,
        dict[type[GameObject], tuple[pg.Rect, Callable]],
    ] = dataclass_field(default_factory=dict)
    sell_button: pg.Rect = dataclass_field(init=False)
    current_tab = "Units"
    production_timer: float | None = None

    def __post_init__(self) -> None:
        self.surface = pg.Surface((self.WIDTH, SCREEN_HEIGHT - CONSOLE_HEIGHT))

        tab_button_base = pg.Rect(
            (self.MARGIN_X, self.TAB_BUTTONS_POS_Y),
            (self._BUTTON_WIDTH, self.TAB_BUTTON_HEIGHT),
        )
        for i, tab_name in enumerate(["Units", "Buildings", "Defensive"]):
            self.tab_buttons[tab_name] = tab_button_base.move(
                0, i * (self.TAB_BUTTON_HEIGHT + self.BUTTON_SPACING_Y)
            )
            self.buy_buttons[tab_name] = {}

        action_button_base = pg.Rect(
            (self.MARGIN_X, 0),
            (self._BUTTON_WIDTH, self.ACTION_BUTTON_HEIGHT),
        )
        buy_button_base = action_button_base.move(0, self.BUY_BUTTONS_POS_Y)
        for i, (cls, req) in enumerate(
            [
                (
                    Tank,
                    lambda: any(
                        b.team == self.hq.team
                        and isinstance(b, WarFactory)
                        and b.health > 0
                        for b in global_buildings
                    ),
                ),
                (
                    Infantry,
                    lambda: any(
                        b.team == self.hq.team
                        and isinstance(b, Barracks)
                        and b.health > 0
                        for b in global_buildings
                    ),
                ),
                (
                    Harvester,
                    lambda: any(
                        b.team == self.hq.team
                        and isinstance(b, WarFactory)
                        and b.health > 0
                        for b in global_buildings
                    ),
                ),
            ]
        ):
            self.buy_buttons["Units"][cls] = (
                buy_button_base.move(
                    0, i * (self.ACTION_BUTTON_HEIGHT + self.BUTTON_SPACING_Y)
                ),
                lambda: req,
            )

        for i, cls in enumerate([Barracks, WarFactory, PowerPlant, Headquarters]):
            self.buy_buttons["Buildings"][cls] = (
                buy_button_base.move(
                    0, i * (self.ACTION_BUTTON_HEIGHT + self.BUTTON_SPACING_Y)
                ),
                lambda: True,
            )
        self.buy_buttons["Defensive"] = {Turret: (buy_button_base, lambda: True)}
        self.sell_button = action_button_base.move(0, self.SELL_BUTTON_POS_Y)
        self.unit_button_labels = {
            Tank: "Tank",
            Infantry: "Infantry",
            Harvester: "Harvester",
            Barracks: "Barracks",
            WarFactory: "War Factory",
            PowerPlant: "Power Plant",
            Headquarters: "Headquarters",
            Turret: "Turret",
        }

    def _local_pos(self, screen_pos: tuple[int, int]) -> tuple[int, int]:
        """Convert screen position to local position."""
        return screen_pos[0] - SCREEN_WIDTH + self.WIDTH, screen_pos[1]

    def _draw_iron(self, *, y_pos: int) -> None:
        self.surface.blit(
            font.render(
                f"Iron: {self.hq.iron}",
                color=pg.Color("white"),
                antialias=True,
            ),
            (self.MARGIN_X, y_pos),
        )

    def _draw_power(self, *, y_pos: int) -> None:
        color_ = pg.Color("green") if self.hq.has_enough_power else pg.Color("red")
        self.surface.blit(
            font.render(
                f"Power: {self.hq.power_output}/{self.hq.power_usage}",
                color=color_,
                antialias=True,
            ),
            (self.MARGIN_X, y_pos),
        )

    def _draw_tab_button(self, *, rect: pg.Rect, label: str) -> None:
        pg.draw.rect(
            self.surface,
            self.ACTIVE_TAB_COLOR
            if label == self.current_tab
            else self.INACTIVE_TAB_COLOR,
            rect,
            border_radius=self.BUTTON_RADIUS,
        )
        self.surface.blit(
            font.render(label, color=pg.Color("white"), antialias=True),
            (rect.x + 10, rect.y + 10),
        )

    def _draw_buy_button(
        self, *, rect: pg.Rect, unit_cls: type[GameObject], req_fn: Callable
    ) -> None:
        can_produce = self.hq.iron >= unit_cls.COST and req_fn
        buy_fill_color = (
            self.ACTION_ALLOWED_COLOR if can_produce else self.ACTION_BLOCKED_COLOR
        )
        pg.draw.rect(
            self.surface, buy_fill_color, rect, border_radius=self.BUTTON_RADIUS
        )
        self.surface.blit(
            font.render(
                f"{self.unit_button_labels[unit_cls]} ({unit_cls.COST})",
                color=pg.Color("white"),
                antialias=True,
            ),
            (rect.x + 10, rect.y + 10),
        )

    def _draw_sell_button(self, *, rect: pg.Rect) -> None:
        sell_fill_color = (
            self.ACTION_ALLOWED_COLOR
            if selected_building
            else self.ACTION_BLOCKED_COLOR
        )
        pg.draw.rect(
            self.surface,
            sell_fill_color,
            rect,
            border_radius=self.BUTTON_RADIUS,
        )
        self.surface.blit(
            font.render("Sell", color=pg.Color("white"), antialias=True),
            (self.sell_button.x + 10, self.sell_button.y + 10),
        )

    def _draw_production_queue(
        self, *, y_pos: int, own_buildings: Iterable[Building]
    ) -> None:
        if self.hq.production_timer and self.hq.production_queue:
            progress = 1 - self.hq.production_timer / self.hq.get_production_time(
                unit_class=self.hq.production_queue[0], friendly_buildings=own_buildings
            )
            draw_progress_bar(
                surface=self.surface,
                bar_color=pg.Color("green"),
                rect=pg.Rect(
                    (self.MARGIN_X, y_pos),
                    (self._BUTTON_WIDTH, 10),
                ),
                progress=progress,
            )

        for i, unit_class in enumerate(self.hq.production_queue[:5]):
            self.surface.blit(
                font.render(
                    f"{unit_class.__name__} ({unit_class.COST})",
                    color=pg.Color("white"),
                    antialias=True,
                ),
                (self.MARGIN_X, (y_pos + 20) + i * 25),
            )

    def _draw_pending_building(
        self, *, surface_: pg.Surface, mouse_pos: tuple[int, int]
    ) -> None:
        if not self.hq.pending_building:
            raise TypeError("No pending building")

        pending_building_cls_ = self.hq.pending_building
        world_pos = snap_to_grid(camera.screen_to_world(mouse_pos))
        temp_surface = pg.Surface(pending_building_cls_.SIZE, pg.SRCALPHA)
        temp_surface.fill(GDI_COLOR if self.hq.team == Team.GDI else NOD_COLOR)
        temp_surface.set_alpha(100)
        color_ = self.PLACEMENT_INVALID_COLOR
        if is_valid_building_position(
            position=world_pos,
            team=self.hq.team,
            new_building_cls=pending_building_cls_,
            buildings=global_buildings,
        ):
            color_ = self.PLACEMENT_VALID_COLOR

        pg.draw.rect(
            temp_surface,
            color_,
            ((0, 0), pending_building_cls_.SIZE),
            width=3,
        )
        surface_.blit(
            temp_surface,
            (
                mouse_pos[0] - pending_building_cls_.SIZE[0] // 2,
                mouse_pos[1] - pending_building_cls_.SIZE[1] // 2,
            ),
        )

    def draw(self, *, surface_: pg.Surface, own_buildings: Iterable[Building]) -> None:
        """Draw to the `surface_`."""
        self.surface.fill(self.FILL_COLOR)
        pg.draw.rect(self.surface, self.LINE_COLOR, self.surface.get_rect(), width=2)
        self._draw_iron(y_pos=self.IRON_POS_Y)
        self._draw_power(y_pos=self.POWER_POS_Y)

        for tab_name, rect in self.tab_buttons.items():
            self._draw_tab_button(rect=rect, label=tab_name)

        for unit_cls, info in self.buy_buttons[self.current_tab].items():
            rect, req_fn = info
            self._draw_buy_button(rect=rect, unit_cls=unit_cls, req_fn=req_fn)

        self._draw_production_queue(
            y_pos=self.PRODUCTION_QUEUE_POS_Y, own_buildings=own_buildings
        )
        self._draw_sell_button(rect=self.sell_button)

        if self.hq.pending_building:
            self._draw_pending_building(surface_=surface_, mouse_pos=pg.mouse.get_pos())

        surface_.blit(source=self.surface, dest=(SCREEN_WIDTH - self.WIDTH, 0))

    def handle_click(
        self, screen_pos: tuple[int, int], own_buildings: Iterable[Building]
    ) -> bool:
        local_pos = self._local_pos(screen_pos)
        global selected_building
        for tab_name, rect in self.tab_buttons.items():
            if rect.collidepoint(local_pos):
                self.current_tab = tab_name
                return True

        if len(self.hq.production_queue) >= self.MAX_PRODUCTION_QUEUE_LENGTH:
            return False

        for unit_cls, info in self.buy_buttons[self.current_tab].items():
            rect, req_fn = info
            if (
                rect.collidepoint(local_pos)
                and self.hq.iron >= unit_cls.COST
                and req_fn()
            ):
                self.hq.production_queue.append(unit_cls)
                self.hq.iron -= unit_cls.COST
                if not self.hq.production_timer:
                    self.production_timer = self.hq.get_production_time(
                        unit_class=unit_cls, friendly_buildings=own_buildings
                    )
                return True

        if self.sell_button.collidepoint(local_pos) and selected_building:
            self.hq.iron += selected_building.COST // 2
            selected_building.kill()
            selected_building = None
            return True

        return False


if __name__ == "__main__":
    pg.init()
    screen = pg.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    clock = pg.time.Clock()
    font = pg.font.SysFont(None, 24)

    player_units: pg.sprite.Group = pg.sprite.Group()
    ai_units: pg.sprite.Group = pg.sprite.Group()
    global_units: pg.sprite.Group = pg.sprite.Group()
    iron_fields: pg.sprite.Group = pg.sprite.Group()
    global_buildings: pg.sprite.Group = pg.sprite.Group()
    projectiles: pg.sprite.Group = pg.sprite.Group()
    particles: pg.sprite.Group = pg.sprite.Group()
    selected_units: pg.sprite.Group = pg.sprite.Group()

    gdi_hq = Headquarters(x=300, y=300, team=Team.GDI)
    nod_hq = Headquarters(x=MAP_WIDTH - 300, y=MAP_HEIGHT - 300, team=Team.NOD)
    nod_hq.iron = 1500
    interface = ProductionInterface(hq=gdi_hq)
    console = GameConsole()
    fog_of_war = FogOfWar(map_size=(MAP_WIDTH, MAP_HEIGHT), tile_size=TILE_SIZE)

    selected_building = None
    selecting = False
    select_start = None
    select_rect = None
    camera = Camera()
    base_map = pg.Surface((MAP_WIDTH, MAP_HEIGHT))
    # Improved map with grass texture
    for x in range(0, MAP_WIDTH, TILE_SIZE):
        for y in range(0, MAP_HEIGHT, TILE_SIZE):
            color = (0, random.randint(100, 150), 0)
            pg.draw.rect(base_map, color, (x, y, TILE_SIZE, TILE_SIZE))
            if random.random() < 0.1:
                pg.draw.circle(
                    base_map,
                    (0, 80, 0),
                    (x + TILE_SIZE // 2, y + TILE_SIZE // 2),
                    TILE_SIZE // 4,
                )  # Dark spots

    ai = AI(hq=nod_hq, console=console)

    player_units.add(Infantry(350, 300, Team.GDI))
    player_units.add(Infantry(370, 300, Team.GDI))
    player_units.add(Infantry(390, 300, Team.GDI))
    player_units.add(Harvester(400, 400, Team.GDI, gdi_hq))

    ai_units.add(Infantry(2050, 1200, Team.NOD))
    ai_units.add(Infantry(2070, 1200, Team.NOD))
    ai_units.add(Infantry(2090, 1200, Team.NOD))
    ai_units.add(Harvester(2200, 1300, Team.NOD, nod_hq))

    global_units.add(player_units, ai_units)
    global_buildings.add(gdi_hq, nod_hq)
    for _ in range(40):
        iron_fields.add(
            IronField(
                random.randint(100, MAP_WIDTH - 100),
                random.randint(100, MAP_HEIGHT - 100),
            )
        )

    running = True
    while running:
        for event in pg.event.get():
            if event.type == pg.QUIT:
                running = False
            elif event.type == pg.MOUSEBUTTONDOWN:
                world_x, world_y = camera.screen_to_world(event.pos)
                target_x, target_y = event.pos
                if event.button == 1:
                    if gdi_hq.pending_building:
                        snapped_position = snap_to_grid((world_x, world_y))
                        if is_valid_building_position(
                            position=snapped_position,
                            team=gdi_hq.team,
                            new_building_cls=gdi_hq.pending_building,
                            buildings=global_buildings,
                        ):
                            gdi_hq.place_building(
                                x=world_x,
                                y=world_y,
                                unit_cls=gdi_hq.pending_building,
                                all_buildings=global_buildings,
                            )
                        continue

                    if interface.handle_click(
                        event.pos,
                        own_buildings=[
                            b for b in global_buildings if b.team == Team.GDI
                        ],
                    ):
                        continue

                    clicked_building = next(
                        (
                            b
                            for b in global_buildings
                            if b.team == Team.GDI
                            and camera.apply(b.rect).collidepoint(target_x, target_y)
                        ),
                        None,
                    )
                    if clicked_building:
                        selected_building = clicked_building
                    else:
                        selected_building = None
                        selecting = True
                        select_start = event.pos
                        select_rect = pg.Rect(target_x, target_y, 0, 0)
                elif event.button == 3:
                    if gdi_hq.pending_building:
                        gdi_hq.pending_building = gdi_hq.pending_building_pos = None
                        if gdi_hq.production_queue and gdi_hq.has_enough_power:
                            gdi_hq.production_timer = gdi_hq.get_production_time(
                                unit_class=gdi_hq.production_queue[0],
                                friendly_buildings=[
                                    b for b in global_buildings if b.team == Team.GDI
                                ],
                            )
                        continue
                    clicked_field = next(
                        (
                            f
                            for f in iron_fields
                            if camera.apply(f.rect).collidepoint(target_x, target_y)
                        ),
                        None,
                    )
                    clicked_enemy_unit = next(
                        (
                            u
                            for u in global_units
                            if u.team != Team.GDI
                            and camera.apply(u.rect).collidepoint(target_x, target_y)
                        ),
                        None,
                    )
                    clicked_enemy_building = next(
                        (
                            b
                            for b in global_buildings
                            if b.team != Team.GDI
                            and camera.apply(b.rect).collidepoint(target_x, target_y)
                        ),
                        None,
                    )
                    if selected_units:
                        group_center = (
                            sum(u.rect.centerx for u in selected_units)
                            / len(selected_units),
                            sum(u.rect.centery for u in selected_units)
                            / len(selected_units),
                        )
                        formation_positions = calculate_formation_positions(
                            center=(world_x, world_y),
                            target=(world_x, world_y),
                            num_units=len(selected_units),
                        )
                        for unit, pos in zip(selected_units, formation_positions):
                            unit.target = pos
                            unit.formation_target = pos
                            unit.target_unit = None
                            if clicked_enemy_unit:
                                unit.target_unit = clicked_enemy_unit
                                unit.target = clicked_enemy_unit.rect.center
                            elif clicked_enemy_building:
                                unit.target_unit = clicked_enemy_building
                                unit.target = clicked_enemy_building.rect.center
                            elif clicked_field:
                                unit.target = clicked_field.rect.center
                                unit.formation_target = None
            elif event.type == pg.MOUSEMOTION and selecting:
                current_pos = event.pos
                if not select_start:
                    raise TypeError("No selection rect start point")
                    # Temporary handling, review later

                select_rect = pg.Rect(
                    min(select_start[0], current_pos[0]),
                    min(select_start[1], current_pos[1]),
                    abs(current_pos[0] - select_start[0]),
                    abs(current_pos[1] - select_start[1]),
                )
            elif event.type == pg.MOUSEBUTTONUP and event.button == 1 and selecting:
                if not select_start:
                    raise TypeError("No selection rect start point")
                    # Temporary handling, review later

                selecting = False
                for unit in player_units:
                    unit.selected = False
                selected_units.empty()
                world_start = camera.screen_to_world(select_start)
                world_end = camera.screen_to_world(event.pos)
                world_rect = pg.Rect(
                    min(world_start[0], world_end[0]),
                    min(world_start[1], world_end[1]),
                    abs(world_end[0] - world_start[0]),
                    abs(world_end[1] - world_start[1]),
                )
                for unit in player_units:
                    if world_rect.colliderect(unit.rect):
                        unit.selected = True
                        selected_units.add(unit)
            console.handle_event(event)

        camera.update(
            selected_units.sprites(), pg.mouse.get_pos(), interface.surface.get_rect()
        )
        for unit in global_units:
            if isinstance(unit, Harvester):
                if unit.team == Team.GDI:
                    unit.update(enemy_units=ai_units, iron_fields=iron_fields)
                else:
                    unit.update(enemy_units=player_units, iron_fields=iron_fields)
            else:
                unit.update()

        iron_fields.update()
        for building in global_buildings:
            if isinstance(building, Headquarters):
                if building.team == Team.GDI:
                    building.update(
                        particles=particles,
                        friendly_units=player_units,
                        friendly_buildings=[
                            b for b in global_buildings if b.team == Team.GDI
                        ],
                        all_units=global_units,
                    )
                else:
                    building.update(
                        particles=particles,
                        friendly_units=ai_units,
                        friendly_buildings=[
                            b for b in global_buildings if b.team != Team.GDI
                        ],
                        all_units=global_units,
                    )

            elif isinstance(building, Turret):
                if building.team == Team.GDI:
                    building.update(
                        particles=particles,
                        projectiles=projectiles,
                        enemy_units=ai_units,
                    )
                else:
                    building.update(
                        particles=particles,
                        projectiles=projectiles,
                        enemy_units=player_units,
                    )
            else:
                building.update(particles=particles)

        projectiles.update(particles)
        particles.update()
        handle_collisions(global_units)
        handle_attacks(
            team_units=player_units,
            all_units=global_units,
            all_buildings=global_buildings,
            projectiles=projectiles,
            particles=particles,
        )
        handle_attacks(
            team_units=ai_units,
            all_units=global_units,
            all_buildings=global_buildings,
            projectiles=projectiles,
            particles=particles,
        )
        handle_projectiles(
            projectiles=projectiles,
            all_units=global_units,
            all_buildings=global_buildings,
        )
        ai.update(
            friendly_units=ai_units.sprites(),
            friendly_buildings=[b for b in global_buildings if b.team != Team.GDI],
            enemy_units=player_units.sprites(),
            enemy_buildings=[b for b in global_buildings if b.team == Team.GDI],
            iron_fields=iron_fields.sprites(),
            all_buildings=global_buildings,
        )
        fog_of_war.update_visibility(player_units, global_buildings, Team.GDI)
        draw(surface_=screen, font_=font)
        for unit in global_units:
            unit.under_attack = False

        pg.display.flip()
        clock.tick(60)

    pg.quit()
