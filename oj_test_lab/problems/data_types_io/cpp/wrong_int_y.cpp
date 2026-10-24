#include <iostream>
#include <iomanip>

int main() {
    int x;
    int y;  // BUG: should be long long
    char c;
    float f;
    double d;
    std::cin >> x >> y >> c >> f >> d;
    std::cout << x << "\n";
    std::cout << y << "\n";
    std::cout << c << "\n";
    std::cout << std::fixed << std::setprecision(2) << f << "\n";
    std::cout << std::fixed << std::setprecision(9) << d << "\n";
    return 0;
}
