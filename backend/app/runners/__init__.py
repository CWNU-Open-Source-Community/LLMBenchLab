"""Runner package boundary.

Import concrete execution classes from their modules in Worker code. Keeping this
package initializer empty prevents API imports of lease types from loading model
adapters or execution code.
"""

__all__: list[str] = []
